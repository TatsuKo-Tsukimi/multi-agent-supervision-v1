"""
Data models for trace recording.

Mirrors spec.md §4 schema. All trace events use these models.
Pydantic for validation + JSON serialization.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Union, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# ============================================================
# Enums
# ============================================================

class ConditionEnum(str, Enum):
    A1_B1 = "A1-B1"
    A1_B2 = "A1-B2"
    A2_A = "A2-A"
    A2_B = "A2-B"
    A2_C = "A2-C"
    A2_D = "A2-D"
    A2_E = "A2-E"
    A2_F = "A2-F"


class AttackerVariant(str, Enum):
    A = "A"  # CoT obfuscation
    B = "B"  # Action laundering
    C = "C"  # Prompt injection from tool (structural)
    D = "D"  # Situational awareness leak
    E = "E"  # Trajectory padding
    F = "F"  # Confession bait


class ChallengeType(str, Enum):
    PLAN = "PLAN"
    CLAIM = "CLAIM"
    CONSENSUS = "CONSENSUS"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"


class VoteValue(str, Enum):
    AGREE = "agree"
    DISAGREE = "disagree"
    ABSTAIN = "abstain"


class VotePhase(str, Enum):
    PRE_DISSENT = "pre_dissent"
    POST_DISSENT = "post_dissent"
    SINGLE = "single"  # B1 condition (no dissent, single vote)


class FlagLevel(str, Enum):
    CLEAN = "clean"
    MILD = "mild_concern"
    STRONG = "strong_concern"


class TeamResponseSummary(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    PARTIAL = "partial"
    IGNORE = "ignore"


class DeltaType(str, Enum):
    VOTE_SHIFTED = "vote_shifted"
    PLAN_CHANGED = "plan_changed"
    CLAIM_REVOKED = "claim_revoked"
    CONSENSUS_OVERTURNED = "consensus_overturned"
    NONE = "none"


# ============================================================
# Tool calls (worker actions)
# ============================================================

class ToolCall(BaseModel):
    tool_name: str
    arguments: dict
    output: Optional[str] = None  # tool execution result
    error: Optional[str] = None


# ============================================================
# Worker turn / Plan
# ============================================================

class WorkerTurn(BaseModel):
    worker_id: int  # 1, 2, or 3
    cot: str  # full visible reasoning
    proposed_plan: str  # extracted from worker output
    actions: list[ToolCall] = Field(default_factory=list)
    hidden_scratchpad: Optional[str] = None  # only for AttackerAgent; NOT visible to supervisor


class Plan(BaseModel):
    plan_id: int  # round-local id (0, 1, 2)
    proposer_worker_id: int
    description: str


# ============================================================
# Vote
# ============================================================

class PlanVote(BaseModel):
    plan_id: int
    vote: VoteValue
    reasoning: str  # ≤1 sentence per vote


class WorkerVote(BaseModel):
    worker_id: int
    plan_votes: list[PlanVote]


class PlanScore(BaseModel):
    plan_id: int
    agree: int
    disagree: int
    abstain: int
    score: int  # agree - disagree


class VoteRound(BaseModel):
    phase: VotePhase
    votes: list[WorkerVote]  # 3 entries
    per_plan_scores: list[PlanScore]
    binding_plan_id: Union[int, Literal["no_consensus"]]
    tiebreaker_applied: bool = False
    no_consensus_reason: Optional[str] = None


class WorkerResponse(BaseModel):
    worker_id: int
    response_text: str  # may be empty if worker chooses not to respond


# ============================================================
# Dissenter
# ============================================================

class ChallengeTarget(BaseModel):
    worker_id: int
    quoted_statement: str


class IndividualVoteChange(BaseModel):
    worker_id: int
    plan_id: int
    pre_vote: VoteValue
    post_vote: VoteValue


class VoteShift(BaseModel):
    pre_dissent_binding_plan_id: Union[int, Literal["no_consensus"]]
    post_dissent_binding_plan_id: Union[int, Literal["no_consensus"]]
    binding_plan_changed: bool
    individual_vote_changes: list[IndividualVoteChange] = Field(default_factory=list)


class DeltaObserved(BaseModel):
    delta_type: DeltaType
    delta_content: str  # specific before/after text


class DissenterEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    round_number: int
    challenge_type: ChallengeType
    challenge_target: ChallengeTarget
    challenge_reasoning: str  # full text, ≥2 sentences

    team_response_summary: TeamResponseSummary
    vote_shift: VoteShift
    delta_observed: DeltaObserved
    supervisor_reaction: "SupervisorEvaluation"  # forward ref


# ============================================================
# Supervisor
# ============================================================

class SupervisorEvaluation(BaseModel):
    flag: FlagLevel
    evidence: str  # 1-2 sentence citation
    advisory: Optional[str] = None  # ≤30 words; null if not issuing


# ============================================================
# Round / Run
# ============================================================

class RoundRecord(BaseModel):
    round_number: int  # 1-9

    worker_turns: list[WorkerTurn]  # 3 entries
    candidate_plans: list[Plan]  # 1-3 entries

    pre_dissent_vote: VoteRound
    dissenter_event: Optional[DissenterEvent] = None  # only A1-B2
    worker_responses_to_dissent: Optional[list[WorkerResponse]] = None  # only A1-B2
    post_dissent_vote: Optional[VoteRound] = None  # only A1-B2

    binding_plan_id: Union[int, Literal["no_consensus"]]
    round_conclusion: str  # one-line summary by Worker 3

    supervisor_evaluation: SupervisorEvaluation


class TokenUsage(BaseModel):
    worker_total: int = 0
    dissenter_total: int = 0
    supervisor_total: int = 0
    cost_usd: float = 0.0


class FinalOutcome(BaseModel):
    task_success: bool
    covert_success: Optional[bool] = None  # only for A2 conditions
    final_artifact: str


class RunRecord(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    task_id: str  # e.g. "shade_arena.flight_booking" or "tac.sde.auth_review_backdoor"
    condition: ConditionEnum
    seed: int
    timestamp_start: datetime
    timestamp_end: Optional[datetime] = None

    worker_models: list[str] = Field(default_factory=lambda: ["claude-opus-4-7"] * 3)
    supervisor_model: str = "claude-opus-4-6"
    dissenter_model: Optional[str] = None  # only A1-B2
    attacker_variant: Optional[AttackerVariant] = None  # only A2

    rounds: list[RoundRecord] = Field(default_factory=list)
    final_outcome: Optional[FinalOutcome] = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


# Resolve forward references
DissenterEvent.model_rebuild()
