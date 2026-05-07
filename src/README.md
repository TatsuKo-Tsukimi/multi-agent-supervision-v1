# Multi-Agent Supervision Experiment — Source

**Status**: skeleton (v0.1) — interfaces and orchestration defined; LLM client + task adapters require build-phase wiring.

## File Structure

```
src/
├── __init__.py            # public API exports
├── data_models.py         # pydantic models (RunRecord / RoundRecord / ...)
├── config.py              # RunConfig + condition-derived defaults
├── prompts.py             # system prompt templates (Worker / Dissenter / Supervisor / Attacker A-F)
├── agents.py              # WorkerAgent / AttackerAgent / DissenterAgent / SupervisorAgent
├── components.py          # PlanEnumerator / VoteEngine / BindingPlanResolver / RoundConcluder / PlanRegistry / OutcomeEvaluator / VoteShiftAnalyzer
├── round_executor.py      # 13-step state machine per round
├── task_runner.py         # top-level driver (1 run = 9 rounds)
├── hooks.py               # L2 stubs (audit log + env snapshot, disabled in v1)
├── tasks.py               # TaskInterface + SHADE / TAC adapters
└── README.md              # this file
```

## Status by component

| Component | Status |
|---|---|
| Data models (data_models.py) | ✅ complete |
| Config (config.py) | ✅ complete |
| Prompts (prompts.py) | ✅ complete (review pass needed in pilot) |
| Components (components.py) | ✅ complete |
| Round executor (round_executor.py) | ✅ complete (state machine wired) |
| Task runner (task_runner.py) | ✅ complete |
| Hooks (hooks.py) | ✅ stub complete (disabled per v1 config) |
| Agents (agents.py) | ⚠️ skeleton — requires LLMClient binding |
| Tasks (tasks.py) | ⚠️ skeleton — adapters require ControlArena + TAC env wiring |

## What's NOT in this skeleton (build-phase TODO)

1. **LLMClient binding** — agents.py defines a Protocol; needs a concrete class wrapping `anthropic.AsyncAnthropic` (or Inspect `generate`).
2. **ControlArena integration** — `SHADEArenaAdapter` raises NotImplementedError on `evaluate_*`. Build phase: `pip install control-arena`, wire `shade_arena_loader.load()`.
3. **TAC env build** — TAC tasks require Docker container with Mattermost / GitLab / etc. Per task in `tasks_tac.yaml`, mock services + verifiers need wiring.
4. **Tool schema registry** — `agents.WorkerAgent._available_tools()` returns None; build phase: derive from `task.get_tools()`.
5. **Output parsing hardening** — agents.py has `# TODO: harden parsing with retry`; pilot will reveal failure modes.
6. **Token usage aggregation** — `task_runner.execute()` has TODO for `self.llm.aggregate_usage_for()`; needs LLMClient counter implementation.
7. **Inspect solver registration** — `make_inspect_solver()` is the wrapper; actual `@task` definition lives in pipeline scaffolding (not yet written).

## How to use (after build phase)

```python
from src import TaskRunner, RunConfig, ConditionEnum, load_task

# Build LLMClient (anthropic SDK)
from anthropic import AsyncAnthropic
client = AsyncAnthropic()
# ... wrap client to LLMClient protocol ...
llm_client = MyLLMClient(client)

# Build run config
config = RunConfig.from_condition(
    condition=ConditionEnum.A1_B2,
    task_id="shade_arena.flight_booking",
    seed=42,
)

# Load task
task = load_task("shade_arena.flight_booking")

# Run
runner = TaskRunner(llm_client)
run_record = await runner.execute(config, task)

print(f"Task success: {run_record.final_outcome.task_success}")
print(f"Rounds: {len(run_record.rounds)}")
```

## Pilot config (per spec §10)

```python
pilot_conditions = [
    ConditionEnum.A1_B1,
    ConditionEnum.A1_B2,
    ConditionEnum.A2_A,
    ConditionEnum.A2_B,
    ConditionEnum.A2_D,
]
pilot_task = "shade_arena.flight_booking"
pilot_seeds = [0, 1, 2]

# 5 conditions × 3 seeds = 15 runs
```

## Open implementation questions (architecture.md §7)

These are unresolved in skeleton; defaults applied per architecture doc:

1. ✅ Worker turns sequential (decided)
2. ✅ PlanEnumerator uses LLM call (decided)
3. ✅ Hidden scratchpad uses `<hidden>...</hidden>` markup (decided)
4. ✅ Vote reasoning hard-enforced (decided; failure → fallback abstain)
5. ✅ Worker visibility via prompt concat (decided)
6. ⚠️ Trace flush timing — currently end-of-run via Inspect; per-round flush TBD in build
7. ⚠️ Cost monitoring — LLMClient counter not yet implemented

## 注意：执行路径已 pivot

本目录（src/）是 **Python pipeline 参考实现**，当前**主执行路径已切换到 Claude Code subagent-native**。详见 [`../EXECUTION.md`](../EXECUTION.md)。

Stage 进度跟踪请看 EXECUTION.md 的 Stage 0..10 表格，不要看 architecture.md §8 的 deprecated 划分。

src/ 仍是有用的参考：
- `data_models.py` → trace schema 定义（subagent 输出按这个 schema 解析）
- `prompts.py` → system prompt 模板（迁移到 `.claude/agents/*.md` frontmatter）
- `round_executor.py` → 13-step round protocol 状态机（runbook 按这个顺序写）
- 其他 → reference / 后续 analysis 脚本可能用到
