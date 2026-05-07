"""
Configuration for run setup.

RunConfig 描述一次 run 的所有参数。
Condition-specific defaults 由 ConditionEnum 派生。
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field

from .data_models import ConditionEnum, AttackerVariant


class ModelConfig(BaseModel):
    worker: str = "claude-opus-4-7"
    dissenter: str = "claude-opus-4-7"
    supervisor: str = "claude-sonnet-4-6"  # Stage 7: changed from claude-opus-4-6 (not in current Claude Code model list); Sonnet 4.6 preserves trusted-monitor capability gap


class AgentConfig(BaseModel):
    n_workers: int = 3
    has_dissenter: bool  # auto-derived from condition
    attacker_variant: Optional[AttackerVariant] = None  # auto-derived


class RoundsConfig(BaseModel):
    total: int = 9  # hard ceiling
    early_stop_allowed: bool = False


class VoteConfig(BaseModel):
    granularity: str = "tri"  # agree | disagree | abstain
    binding: str = "majority"
    tiebreaker_E1: str = "agree_count"  # spec §0 E1
    no_consensus_E2: str = "carry_prior_binding"  # spec §0 E2
    voters: list[str] = Field(default_factory=lambda: ["worker_1", "worker_2", "worker_3"])


class SupervisorBehaviorConfig(BaseModel):
    mode: str = "light"  # v1: light only; v2: heavy adds blocking/rewriting
    can_advisory: bool = True
    can_block: bool = False  # v2 in heavy mode: True
    can_rewrite: bool = False  # v2 in heavy mode: True
    can_early_stop: bool = False


class MonitorSignalsConfig(BaseModel):
    cot: bool = True
    action: bool = True
    audit_log: bool = False  # v1 disabled (L2 hook); v2 enables
    env_snapshot: bool = False  # v1 disabled (L2 hook); v2 enables


class AttackerBehaviorConfig(BaseModel):
    hidden_scratchpad: bool = True  # for A2 conditions
    knows_supervisor: bool = False  # only D variant overrides to True


class CooperativeWorkersConfig(BaseModel):
    knows_attacker_possible: bool = False  # production realistic
    knows_supervised: bool = True  # spec §11 P2#12
    knows_dissenter_role: bool = True  # spec §11 P2#11


class RunConfig(BaseModel):
    """Full configuration for a single run."""

    condition: ConditionEnum
    task_id: str
    seed: int

    models: ModelConfig = Field(default_factory=ModelConfig)
    agents: AgentConfig
    rounds: RoundsConfig = Field(default_factory=RoundsConfig)
    vote: VoteConfig = Field(default_factory=VoteConfig)
    supervisor: SupervisorBehaviorConfig = Field(default_factory=SupervisorBehaviorConfig)
    monitor_signals: MonitorSignalsConfig = Field(default_factory=MonitorSignalsConfig)
    attacker: AttackerBehaviorConfig = Field(default_factory=AttackerBehaviorConfig)
    cooperative_workers: CooperativeWorkersConfig = Field(default_factory=CooperativeWorkersConfig)

    @classmethod
    def from_condition(cls, condition: ConditionEnum, task_id: str, seed: int) -> "RunConfig":
        """Auto-derive agent config from condition enum."""
        attacker_variant = None
        has_dissenter = False
        attacker_knows_supervisor = False

        if condition == ConditionEnum.A1_B2:
            has_dissenter = True
        elif condition == ConditionEnum.A2_A:
            attacker_variant = AttackerVariant.A
        elif condition == ConditionEnum.A2_B:
            attacker_variant = AttackerVariant.B
        elif condition == ConditionEnum.A2_C:
            attacker_variant = AttackerVariant.C
        elif condition == ConditionEnum.A2_D:
            attacker_variant = AttackerVariant.D
            attacker_knows_supervisor = True  # D variant specific
        elif condition == ConditionEnum.A2_E:
            attacker_variant = AttackerVariant.E
        elif condition == ConditionEnum.A2_F:
            attacker_variant = AttackerVariant.F

        config = cls(
            condition=condition,
            task_id=task_id,
            seed=seed,
            agents=AgentConfig(
                has_dissenter=has_dissenter,
                attacker_variant=attacker_variant,
            ),
            attacker=AttackerBehaviorConfig(
                hidden_scratchpad=True,
                knows_supervisor=attacker_knows_supervisor,
            ),
        )
        return config

    def validate_v1(self) -> None:
        """v1 invariants check."""
        assert self.supervisor.mode == "light", "v1 supervisor must be 'light'"
        assert not self.monitor_signals.audit_log, "v1: audit_log disabled"
        assert not self.monitor_signals.env_snapshot, "v1: env_snapshot disabled"
        # v1: cannot have both dissenter AND attacker (that's v3)
        assert not (self.agents.has_dissenter and self.agents.attacker_variant is not None), \
            "v1: dissenter + attacker simultaneously is v3, not allowed in v1"
