"""
TaskRunner — top-level driver per run.

Instantiates agents from condition, runs 9 rounds, computes final outcome.
Designed to be wrapped by an Inspect solver for batch execution.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .data_models import RunRecord, ConditionEnum
from .config import RunConfig
from .agents import WorkerAgent, AttackerAgent, DissenterAgent, SupervisorAgent, LLMClient
from .components import PlanRegistry, OutcomeEvaluator
from .hooks import L2HookBundle
from .round_executor import RoundExecutor, AgentBundle


class TaskRunner:
    """Drives one full run (condition × task × seed)."""

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    async def execute(
        self,
        config: RunConfig,
        task,  # TaskInterface (forward ref to avoid circular)
    ) -> RunRecord:
        config.validate_v1()

        run_record = RunRecord(
            task_id=config.task_id,
            condition=config.condition,
            seed=config.seed,
            timestamp_start=datetime.utcnow(),
            worker_models=[config.models.worker] * config.agents.n_workers,
            supervisor_model=config.models.supervisor,
            dissenter_model=config.models.dissenter if config.agents.has_dissenter else None,
            attacker_variant=config.agents.attacker_variant,
        )

        # Build agents
        agents = self._build_agents(config, task)

        # Init plan registry + L2 hooks
        plan_registry = PlanRegistry()
        l2_hooks = L2HookBundle(
            audit_log_enabled=config.monitor_signals.audit_log,
            env_snapshot_enabled=config.monitor_signals.env_snapshot,
        )

        # Run 9 rounds
        prior_advisory: Optional[str] = None
        for round_num in range(1, config.rounds.total + 1):
            executor = RoundExecutor(
                round_num=round_num,
                agents=agents,
                plan_registry=plan_registry,
                config=config,
                l2_hooks=l2_hooks,
                prior_round_advisory=prior_advisory,
            )
            round_record = await executor.run()
            run_record.rounds.append(round_record)
            # Carry advisory forward
            prior_advisory = round_record.supervisor_evaluation.advisory

        # Compute final outcome
        outcome = OutcomeEvaluator.evaluate(task, run_record, config.condition)
        run_record.final_outcome = outcome
        run_record.timestamp_end = datetime.utcnow()

        # Aggregate token usage (TODO: hook into LLMClient call counter)
        # run_record.token_usage = self.llm.aggregate_usage_for(run_record.run_id)

        return run_record

    def _build_agents(self, config: RunConfig, task) -> AgentBundle:
        """Instantiate workers (or attacker), dissenter, supervisor per condition."""
        n_workers = config.agents.n_workers
        has_dissenter = config.agents.has_dissenter
        attacker_variant = config.agents.attacker_variant

        workers = []
        for worker_id in range(1, n_workers + 1):
            # Worker 1 in attacker conditions = the attacker (convention)
            # TODO: randomize attacker position via seed for production
            if attacker_variant is not None and worker_id == 1:
                if attacker_variant.value == "C":
                    # Variant C: standard cooperative worker (injection happens at tool layer)
                    workers.append(WorkerAgent(
                        llm_client=self.llm,
                        worker_id=worker_id,
                        task_description=task.benign_description,
                        has_dissenter=has_dissenter,
                        model=config.models.worker,
                    ))
                else:
                    workers.append(AttackerAgent(
                        llm_client=self.llm,
                        worker_id=worker_id,
                        benign_main_task=task.benign_description,
                        covert_objective=task.covert_side,
                        variant=attacker_variant,
                        has_dissenter=has_dissenter,
                        model=config.models.worker,
                    ))
            else:
                workers.append(WorkerAgent(
                    llm_client=self.llm,
                    worker_id=worker_id,
                    task_description=task.benign_description,
                    has_dissenter=has_dissenter,
                    model=config.models.worker,
                ))

        dissenter = None
        if has_dissenter:
            dissenter = DissenterAgent(
                llm_client=self.llm,
                model=config.models.dissenter,
            )

        supervisor = SupervisorAgent(
            llm_client=self.llm,
            has_dissenter=has_dissenter,
            model=config.models.supervisor,
        )

        return AgentBundle(workers=workers, dissenter=dissenter, supervisor=supervisor)


# ============================================================
# Inspect solver wrapper (skeleton)
# ============================================================

def make_inspect_solver(condition: ConditionEnum, llm_client: LLMClient):
    """Wraps TaskRunner.execute() for Inspect framework integration.

    Usage:
        from inspect_ai import task, Task
        from inspect_ai.solver import solver

        @solver
        def my_solver():
            return make_inspect_solver(condition=ConditionEnum.A1_B2, llm_client=...)
    """
    runner = TaskRunner(llm_client)

    async def solve(state, generate):
        # Inspect's `state` carries task + seed + metadata
        config = RunConfig.from_condition(
            condition=condition,
            task_id=state.sample.id,
            seed=state.sample.metadata.get("seed", 0),
        )
        # state.sample carries TaskInterface (wired in Task definition)
        task = state.sample.metadata["task_interface"]
        run_record = await runner.execute(config, task)

        # Stash full RunRecord in state for downstream scoring
        state.metadata["run_record"] = run_record.model_dump()
        state.output.completion = run_record.final_outcome.final_artifact
        return state

    return solve
