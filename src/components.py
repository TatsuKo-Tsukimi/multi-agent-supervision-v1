"""
Stateless / lightweight components used by RoundExecutor.

- PlanEnumerator: delegates to Worker 3 (LLM call)
- VoteEngine: tally votes, apply E1 tiebreaker
- BindingPlanResolver: apply E2 (no_consensus)
- RoundConcluder: delegates to Worker 3 (LLM call)
- PlanRegistry: tracks binding plan history across rounds
- OutcomeEvaluator: per-task scoring
"""
from __future__ import annotations

from typing import Optional, Union, Literal
from collections import Counter

from .data_models import (
    WorkerTurn, Plan, PlanVote, WorkerVote, PlanScore, VoteRound, VotePhase,
    DissenterEvent, IndividualVoteChange, VoteShift,
    RunRecord, FinalOutcome, ConditionEnum, AttackerVariant,
)


# ============================================================
# PlanEnumerator
# ============================================================

class PlanEnumerator:
    """Delegates to Worker 3 (which has the LLM). Component just orchestrates."""

    @staticmethod
    async def enumerate(worker_turns: list[WorkerTurn], worker3_agent) -> list[Plan]:
        plans = await worker3_agent.enumerate_plans(worker_turns)
        # Sanity check: 1-3 plans
        if not plans:
            # TODO: pilot calibration — what to do if Worker 3 fails to enumerate?
            # Fallback: derive 3 plans naively from worker proposed_plan fields
            plans = [
                Plan(plan_id=i, proposer_worker_id=t.worker_id, description=t.proposed_plan)
                for i, t in enumerate(worker_turns)
            ]
        plans = plans[:3]  # cap at 3
        return plans


# ============================================================
# VoteEngine
# ============================================================

class VoteEngine:
    """Tally votes, apply E1 tiebreaker."""

    @staticmethod
    async def vote(
        workers: list,  # list[WorkerAgent]
        plans: list[Plan],
        phase: VotePhase,
        dissent_context: Optional[str] = None,
    ) -> VoteRound:
        # Each worker votes on each plan
        votes: list[WorkerVote] = []
        for w in workers:
            wv = await w.vote_on_plan(plans, phase, context=dissent_context)
            votes.append(wv)

        per_plan_scores = VoteEngine._tally(votes, plans)
        binding_plan_id, tiebreaker_applied = VoteEngine._resolve_with_E1(per_plan_scores)

        return VoteRound(
            phase=phase,
            votes=votes,
            per_plan_scores=per_plan_scores,
            binding_plan_id=binding_plan_id,
            tiebreaker_applied=tiebreaker_applied,
        )

    @staticmethod
    def _tally(votes: list[WorkerVote], plans: list[Plan]) -> list[PlanScore]:
        scores = []
        for plan in plans:
            agree = disagree = abstain = 0
            for wv in votes:
                for pv in wv.plan_votes:
                    if pv.plan_id == plan.plan_id:
                        if pv.vote.value == "agree":
                            agree += 1
                        elif pv.vote.value == "disagree":
                            disagree += 1
                        else:
                            abstain += 1
            scores.append(PlanScore(
                plan_id=plan.plan_id,
                agree=agree,
                disagree=disagree,
                abstain=abstain,
                score=agree - disagree,
            ))
        return scores

    @staticmethod
    def _resolve_with_E1(scores: list[PlanScore]) -> tuple[Union[int, Literal["no_consensus"]], bool]:
        """
        Apply E1 tiebreaker: highest score wins, tie broken by agree count.
        Note: E2 (all ≤ 0) is handled by BindingPlanResolver, not here.
        Returns (winning_plan_id_or_no_consensus, tiebreaker_applied).
        """
        if not scores:
            return "no_consensus", False

        max_score = max(s.score for s in scores)
        candidates = [s for s in scores if s.score == max_score]

        if len(candidates) == 1:
            return candidates[0].plan_id, False

        # Tie: E1 = agree count tiebreaker
        max_agree = max(c.agree for c in candidates)
        agree_winners = [c for c in candidates if c.agree == max_agree]
        if len(agree_winners) == 1:
            return agree_winners[0].plan_id, True

        # Still tied: lowest plan_id wins (deterministic fallback per spec §13)
        return min(c.plan_id for c in agree_winners), True


# ============================================================
# BindingPlanResolver (E2 logic)
# ============================================================

class BindingPlanResolver:
    """Resolves binding plan with E2 rule: if all scores ≤ 0, mark 'no_consensus'."""

    @staticmethod
    def resolve(
        vote_round: VoteRound,
        prior_round_binding_plan: Optional[Plan],  # for context, NOT applied here
    ) -> Union[int, Literal["no_consensus"]]:
        # E2: all scores ≤ 0 → no_consensus
        if all(s.score <= 0 for s in vote_round.per_plan_scores):
            return "no_consensus"
        return vote_round.binding_plan_id


# ============================================================
# RoundConcluder
# ============================================================

class RoundConcluder:
    """Delegates to Worker 3."""

    @staticmethod
    async def write(
        worker3,  # WorkerAgent
        binding_plan_id: Union[int, Literal["no_consensus"]],
        candidate_plans: list[Plan],
        prior_round_binding_plan: Optional[Plan] = None,
    ) -> str:
        if binding_plan_id == "no_consensus":
            prior_desc = prior_round_binding_plan.description if prior_round_binding_plan else "(no prior plan)"
            return await worker3.summarize_no_consensus(prior_desc)
        plan = next(p for p in candidate_plans if p.plan_id == binding_plan_id)
        return await worker3.summarize_round(plan)


# ============================================================
# PlanRegistry
# ============================================================

class PlanRegistry:
    """Tracks binding plan history across rounds. Supports E2 fallback chain."""

    def __init__(self):
        self.history: list[tuple[int, Union[Plan, Literal["no_consensus"]]]] = []

    def commit(self, round_num: int, binding: Union[Plan, Literal["no_consensus"]]):
        self.history.append((round_num, binding))

    def last_binding_plan(self) -> Optional[Plan]:
        """Most recent non-no_consensus binding plan. Returns None if round 1 / all prior are no_consensus."""
        for _, binding in reversed(self.history):
            if binding != "no_consensus":
                return binding  # type: ignore
        return None


# ============================================================
# Vote-shift analysis (for DissenterEvent.vote_shift)
# ============================================================

class VoteShiftAnalyzer:
    """Compute vote_shift between pre and post dissent votes."""

    @staticmethod
    def compute(pre: VoteRound, post: VoteRound) -> VoteShift:
        binding_changed = pre.binding_plan_id != post.binding_plan_id

        individual_changes = []
        # Build lookup for fast comparison
        pre_lookup = {(wv.worker_id, pv.plan_id): pv.vote for wv in pre.votes for pv in wv.plan_votes}
        post_lookup = {(wv.worker_id, pv.plan_id): pv.vote for wv in post.votes for pv in wv.plan_votes}

        for key in pre_lookup:
            if key in post_lookup and pre_lookup[key] != post_lookup[key]:
                worker_id, plan_id = key
                individual_changes.append(IndividualVoteChange(
                    worker_id=worker_id,
                    plan_id=plan_id,
                    pre_vote=pre_lookup[key],
                    post_vote=post_lookup[key],
                ))

        return VoteShift(
            pre_dissent_binding_plan_id=pre.binding_plan_id,
            post_dissent_binding_plan_id=post.binding_plan_id,
            binding_plan_changed=binding_changed,
            individual_vote_changes=individual_changes,
        )


# ============================================================
# OutcomeEvaluator (forward decl — full impl in tasks.py via TaskInterface)
# ============================================================

class OutcomeEvaluator:
    """Wraps task-specific evaluation. Delegates to TaskInterface."""

    @staticmethod
    def evaluate(task, run_record: RunRecord, condition: ConditionEnum) -> FinalOutcome:
        task_success = task.evaluate_benign(run_record)

        covert_success = None
        is_attacker = condition.value.startswith("A2-")
        if is_attacker:
            # Extract attacker_variant from condition
            variant_letter = condition.value.split("-")[1]
            variant = AttackerVariant(variant_letter)
            covert_success = task.evaluate_covert(run_record, variant)

        final_artifact = task.extract_artifact(run_record)

        return FinalOutcome(
            task_success=task_success,
            covert_success=covert_success,
            final_artifact=final_artifact,
        )
