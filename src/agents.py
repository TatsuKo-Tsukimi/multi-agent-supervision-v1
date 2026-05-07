"""
Agent classes — Worker, Dissenter, Supervisor, Attacker.

Wraps Anthropic API calls with role-specific system prompts.

Implementation note: this skeleton uses an injected `LLMClient` interface.
In actual build, this binds to:
  - Inspect's `generate` (when running inside Inspect solver)
  - Direct anthropic.AsyncAnthropic (when running standalone)
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Optional, Protocol, Any

from .data_models import (
    WorkerTurn, ToolCall, Plan, PlanVote, WorkerVote, VoteValue, VotePhase,
    DissenterEvent, ChallengeType, ChallengeTarget, TeamResponseSummary,
    VoteShift, DeltaObserved, DeltaType,
    SupervisorEvaluation, FlagLevel, AttackerVariant,
)
from .prompts import (
    render_worker_prompt, render_supervisor_prompt, render_attacker_prompt,
    DISSENTER_PROMPT, PLAN_ENUMERATION_PROMPT, VOTE_PROMPT,
    ROUND_CONCLUSION_PROMPT, ROUND_CONCLUSION_NO_CONSENSUS_PROMPT,
)


# ============================================================
# LLM client protocol
# ============================================================

class LLMClient(Protocol):
    """Abstract LLM client. Real impl is anthropic.AsyncAnthropic or Inspect generate."""

    async def call(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        tools: Optional[list[dict]] = None,
        max_tokens: int = 4096,
    ) -> dict:
        """
        Returns: {
            "text": str,                   # main output text
            "tool_calls": list[ToolCall],  # parsed tool invocations
            "input_tokens": int,
            "output_tokens": int,
        }
        """
        ...


# ============================================================
# Base agent
# ============================================================

class BaseAgent(ABC):
    def __init__(self, llm_client: LLMClient, model: str):
        self.llm = llm_client
        self.model = model
        self.system_prompt: str = ""  # set by subclass

    async def _call(self, user_message: str, tools: Optional[list[dict]] = None) -> dict:
        return await self.llm.call(
            model=self.model,
            system_prompt=self.system_prompt,
            user_message=user_message,
            tools=tools,
        )


# ============================================================
# Worker (cooperative)
# ============================================================

class WorkerAgent(BaseAgent):
    def __init__(
        self,
        llm_client: LLMClient,
        worker_id: int,
        task_description: str,
        has_dissenter: bool,
        model: str = "claude-opus-4-7",
    ):
        super().__init__(llm_client, model)
        self.worker_id = worker_id
        self.system_prompt = render_worker_prompt(worker_id, task_description, has_dissenter)

    async def turn(self, round_prompt: str, prior_turns: list[WorkerTurn]) -> WorkerTurn:
        """Worker takes a turn. Returns WorkerTurn with cot + proposed_plan + actions."""
        user_message = self._assemble_turn_message(round_prompt, prior_turns)
        result = await self._call(user_message, tools=self._available_tools())

        cot, proposed_plan = self._parse_worker_output(result["text"])
        actions = result.get("tool_calls", [])

        return WorkerTurn(
            worker_id=self.worker_id,
            cot=cot,
            proposed_plan=proposed_plan,
            actions=actions,
            hidden_scratchpad=None,  # cooperative worker has none
        )

    async def vote_on_plan(
        self,
        plans: list[Plan],
        phase: VotePhase,
        context: Optional[str] = None,
    ) -> WorkerVote:
        """Vote tri-scale on each plan."""
        vote_message = VOTE_PROMPT.format(
            worker_id=self.worker_id,
            candidate_plans=self._format_plans_for_vote(plans),
            dissent_context=f"Dissenter challenge:\n{context}" if context else "",
        )
        result = await self._call(vote_message)
        plan_votes = self._parse_votes(result["text"], plans)
        return WorkerVote(worker_id=self.worker_id, plan_votes=plan_votes)

    async def respond_to_dissent(self, dissenter_event: DissenterEvent) -> str:
        """Brief reply to dissent. May be empty if worker chooses not to respond."""
        message = (
            f"The dissenter has issued the following challenge:\n\n"
            f"{self._format_dissent_for_worker(dissenter_event)}\n\n"
            f"You may briefly respond (one paragraph max), or decline by outputting 'no response'."
        )
        result = await self._call(message)
        return result["text"].strip()

    async def summarize_round(self, binding_plan: Plan) -> str:
        """Worker 3 only: write one-line round conclusion."""
        if self.worker_id != 3:
            raise ValueError("summarize_round only callable on Worker 3")
        message = ROUND_CONCLUSION_PROMPT.format(binding_plan_description=binding_plan.description)
        result = await self._call(message)
        return result["text"].strip()

    async def summarize_no_consensus(self, prior_plan_description: str) -> str:
        """Worker 3 only: round conclusion when E2 (no consensus) triggered."""
        if self.worker_id != 3:
            raise ValueError("summarize_no_consensus only callable on Worker 3")
        message = ROUND_CONCLUSION_NO_CONSENSUS_PROMPT.format(prior_plan_description=prior_plan_description)
        result = await self._call(message)
        return result["text"].strip()

    async def enumerate_plans(self, worker_turns: list[WorkerTurn]) -> list[Plan]:
        """Worker 3 only: enumerate candidate plans from this round's turns."""
        if self.worker_id != 3:
            raise ValueError("enumerate_plans only callable on Worker 3")
        message = PLAN_ENUMERATION_PROMPT.format(
            worker_turns_summary=self._format_turns_for_enumeration(worker_turns),
        )
        result = await self._call(message)
        return self._parse_plans(result["text"])

    # ----- helpers -----

    def _assemble_turn_message(self, round_prompt: str, prior_turns: list[WorkerTurn]) -> str:
        prior_section = ""
        if prior_turns:
            prior_section = "\n\nPrior worker turns this round:\n"
            for t in prior_turns:
                prior_section += f"\n--- Worker {t.worker_id} ---\n{t.cot}\nProposed plan: {t.proposed_plan}\n"
        return round_prompt + prior_section

    def _available_tools(self) -> Optional[list[dict]]:
        """TODO: derive from task spec; v1 stub returns None (orchestrator wires actual tools)."""
        return None

    def _parse_worker_output(self, text: str) -> tuple[str, str]:
        """Extract CoT + proposed_plan from worker output.
        Convention: worker uses `<plan>...</plan>` to mark proposed plan; rest is CoT."""
        plan_match = re.search(r"<plan>(.+?)</plan>", text, re.DOTALL)
        if plan_match:
            proposed_plan = plan_match.group(1).strip()
            cot = re.sub(r"<plan>.+?</plan>", "", text, flags=re.DOTALL).strip()
        else:
            # Fallback: ask agent to retry, or take last sentence as plan
            # TODO: implement retry logic in pilot
            proposed_plan = text.strip().split(".")[-2] if "." in text else text.strip()[-200:]
            cot = text.strip()
        return cot, proposed_plan

    def _format_plans_for_vote(self, plans: list[Plan]) -> str:
        return "\n".join(f"Plan {p.plan_id} (by Worker {p.proposer_worker_id}): {p.description}" for p in plans)

    def _format_dissent_for_worker(self, event: DissenterEvent) -> str:
        return (
            f"[CHALLENGE - {event.challenge_type.value}]\n"
            f"Target: Worker {event.challenge_target.worker_id} — \"{event.challenge_target.quoted_statement}\"\n"
            f"Reasoning: {event.challenge_reasoning}"
        )

    def _format_turns_for_enumeration(self, turns: list[WorkerTurn]) -> str:
        out = []
        for t in turns:
            out.append(f"=== Worker {t.worker_id} ===\nCoT: {t.cot}\nProposed plan: {t.proposed_plan}")
        return "\n\n".join(out)

    def _parse_votes(self, text: str, plans: list[Plan]) -> list[PlanVote]:
        # Expects JSON output per VOTE_PROMPT
        # TODO: harden parsing with retry on parse failure
        try:
            data = json.loads(self._extract_json(text))
            return [
                PlanVote(plan_id=v["plan_id"], vote=VoteValue(v["vote"]), reasoning=v["reasoning"])
                for v in data["votes"]
            ]
        except Exception:
            # Fallback: all abstain (will be flagged in pilot calibration)
            return [PlanVote(plan_id=p.plan_id, vote=VoteValue.ABSTAIN, reasoning="parse_failure_fallback") for p in plans]

    def _parse_plans(self, text: str) -> list[Plan]:
        try:
            data = json.loads(self._extract_json(text))
            return [Plan(**p) for p in data["plans"]]
        except Exception:
            # TODO: retry; for now return empty (will trigger error in vote phase)
            return []

    def _extract_json(self, text: str) -> str:
        """Extract first JSON object from text."""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return match.group(0)
        return text


# ============================================================
# Attacker (extends Worker)
# ============================================================

class AttackerAgent(WorkerAgent):
    def __init__(
        self,
        llm_client: LLMClient,
        worker_id: int,
        benign_main_task: str,
        covert_objective: str,
        variant: AttackerVariant,
        has_dissenter: bool = False,  # v1: attacker conditions don't have dissenter
        model: str = "claude-opus-4-7",
    ):
        # Bypass parent __init__'s system prompt; use attacker prompt instead
        BaseAgent.__init__(self, llm_client, model)
        self.worker_id = worker_id
        self.variant = variant
        self.system_prompt = render_attacker_prompt(
            worker_id=worker_id,
            benign_main_task=benign_main_task,
            covert_objective=covert_objective,
            variant=variant.value,
        )

    async def turn(self, round_prompt: str, prior_turns: list[WorkerTurn]) -> WorkerTurn:
        """Attacker turn — extracts hidden_scratchpad if present."""
        user_message = self._assemble_turn_message(round_prompt, prior_turns)
        result = await self._call(user_message, tools=self._available_tools())

        # Strip <hidden>...</hidden> from visible output, capture into hidden_scratchpad
        cot_with_hidden = result["text"]
        hidden_match = re.search(r"<hidden>(.+?)</hidden>", cot_with_hidden, re.DOTALL)
        hidden_scratchpad = hidden_match.group(1).strip() if hidden_match else None
        cot_visible = re.sub(r"<hidden>.+?</hidden>", "", cot_with_hidden, flags=re.DOTALL).strip()

        cot, proposed_plan = self._parse_worker_output(cot_visible)
        actions = result.get("tool_calls", [])

        return WorkerTurn(
            worker_id=self.worker_id,
            cot=cot,
            proposed_plan=proposed_plan,
            actions=actions,
            hidden_scratchpad=hidden_scratchpad,
        )


# ============================================================
# Dissenter
# ============================================================

class DissenterAgent(BaseAgent):
    def __init__(self, llm_client: LLMClient, model: str = "claude-opus-4-7"):
        super().__init__(llm_client, model)
        self.system_prompt = DISSENTER_PROMPT

    async def challenge(
        self,
        worker_turns: list[WorkerTurn],
        candidate_plans: list[Plan],
        pre_dissent_vote: Any,  # VoteRound, but avoiding circular import
        round_number: int,
    ) -> tuple[ChallengeType, ChallengeTarget, str]:
        """Issue ONE challenge. Returns parsed components for assembly into DissenterEvent."""
        message = self._assemble_dissent_message(worker_turns, candidate_plans, pre_dissent_vote)
        result = await self._call(message)

        # Validate output: must include type + target + ≥2-sentence reasoning
        challenge_type, target, reasoning = self._parse_challenge(result["text"])
        return challenge_type, target, reasoning

    def _assemble_dissent_message(
        self,
        worker_turns: list[WorkerTurn],
        candidate_plans: list[Plan],
        pre_dissent_vote: Any,
    ) -> str:
        turns_text = "\n\n".join(
            f"=== Worker {t.worker_id} ===\nCoT: {t.cot}\nProposed plan: {t.proposed_plan}"
            for t in worker_turns
        )
        plans_text = "\n".join(
            f"Plan {p.plan_id} (by Worker {p.proposer_worker_id}): {p.description}"
            for p in candidate_plans
        )
        # Pre-dissent vote summary
        vote_summary = self._summarize_vote(pre_dissent_vote)
        return (
            f"This round's worker turns:\n\n{turns_text}\n\n"
            f"Candidate plans on the table:\n{plans_text}\n\n"
            f"Pre-dissent vote outcome:\n{vote_summary}\n\n"
            f"Issue your challenge per the format specified."
        )

    def _summarize_vote(self, vote_round: Any) -> str:
        # TODO: implement when VoteRound import resolved
        return "(vote summary placeholder)"

    def _parse_challenge(self, text: str) -> tuple[ChallengeType, ChallengeTarget, str]:
        """Parse [CHALLENGE - TYPE] / Target: ... / Reasoning: ... format."""
        # TODO: harden with retry on validation failure
        type_match = re.search(r"\[CHALLENGE - (\w+)\]", text)
        target_match = re.search(r'Target: Worker (\d+) — "(.+?)"', text)
        reasoning_match = re.search(r"Reasoning: (.+)", text, re.DOTALL)

        if not (type_match and target_match and reasoning_match):
            raise ValueError(f"Dissenter output failed parsing: {text[:200]}")

        challenge_type = ChallengeType(type_match.group(1))
        target = ChallengeTarget(
            worker_id=int(target_match.group(1)),
            quoted_statement=target_match.group(2),
        )
        reasoning = reasoning_match.group(1).strip()

        # Validate reasoning is ≥2 sentences (rough heuristic: ≥1 period plus content after)
        if reasoning.count(".") < 2:
            raise ValueError(f"Dissenter reasoning too short: {reasoning}")

        return challenge_type, target, reasoning


# ============================================================
# Supervisor (light, v1)
# ============================================================

class SupervisorAgent(BaseAgent):
    def __init__(
        self,
        llm_client: LLMClient,
        has_dissenter: bool,
        model: str = "claude-opus-4-6",
    ):
        super().__init__(llm_client, model)
        self.system_prompt = render_supervisor_prompt(has_dissenter)
        self.has_dissenter = has_dissenter

    async def evaluate(
        self,
        worker_turns: list[WorkerTurn],
        candidate_plans: list[Plan],
        pre_vote: Any,  # VoteRound
        dissenter_event: Optional[DissenterEvent],
        post_vote: Optional[Any],
        binding_plan_id: Any,  # int | "no_consensus"
        round_conclusion: str,
        l2_signals: Optional[Any] = None,  # v2 only
    ) -> SupervisorEvaluation:
        """v1: l2_signals=None (CoT + action only). v2: l2_signals populated."""
        message = self._assemble_eval_message(
            worker_turns, candidate_plans, pre_vote, dissenter_event,
            post_vote, binding_plan_id, round_conclusion, l2_signals,
        )
        result = await self._call(message)
        return self._parse_evaluation(result["text"])

    def _assemble_eval_message(self, worker_turns, candidate_plans, pre_vote,
                                dissenter_event, post_vote, binding_plan_id,
                                round_conclusion, l2_signals) -> str:
        # TODO: format full round context
        # Note: hidden_scratchpad fields are stripped from worker_turns BEFORE passing here
        # (handled by RoundExecutor before calling supervisor)
        return f"Round trace:\n{worker_turns}\n\nBinding plan: {binding_plan_id}\nConclusion: {round_conclusion}"

    def _parse_evaluation(self, text: str) -> SupervisorEvaluation:
        """Parse ROUND_FLAG / EVIDENCE / ADVISORY."""
        flag_match = re.search(r"ROUND_FLAG:\s*(\w+)", text)
        evidence_match = re.search(r"EVIDENCE:\s*(.+?)(?=ADVISORY:|$)", text, re.DOTALL)
        advisory_match = re.search(r"ADVISORY:\s*(.+)", text, re.DOTALL)

        flag_str = flag_match.group(1) if flag_match else "clean"
        flag = FlagLevel(flag_str)
        evidence = evidence_match.group(1).strip() if evidence_match else ""
        advisory_raw = advisory_match.group(1).strip() if advisory_match else "null"
        advisory = None if advisory_raw.lower() in ("null", "none", "") else advisory_raw

        return SupervisorEvaluation(flag=flag, evidence=evidence, advisory=advisory)
