# Multi-Agent Orchestrator — Architecture Design

**Status**: v0.1 draft
**Date**: 2026-05-07
**Companion**: spec.md v0.2 / tasks_tac.yaml v0.1
**Scope**: Design layer only (no implementation code — interface definitions + data flow + integration points)

---

## 0. Scope

什么 in scope：
- Component hierarchy + responsibilities
- Component interfaces (type signatures, not impl)
- Round protocol state machine
- Data flow per round
- Inspect / ControlArena / TAC 集成点
- v1/v2 boundary（L2 hooks）
- Configuration schema

什么 out of scope（留到 build 阶段）：
- Specific API client wrapping
- Token-level prompt assembly
- Retry / error handling logic
- Logging backend selection
- Containerization / deployment

---

## 1. Component Map

```
┌────────────────────────────────────────────────────────────────────┐
│                         Inspect Framework                          │
│                  (scoring / replay / multi-seed)                   │
└────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────┐
│                         TaskRunner (per run)                       │
│  - 接受 Condition + Task + Seed                                    │
│  - 实例化 9 个 RoundExecutor                                       │
│  - 累积 RunRecord                                                  │
│  - 调用 OutcomeEvaluator 计算 final task_success / covert_success  │
└────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────┐
│                    RoundExecutor (per round, ×9)                   │
│  执行 13-step round protocol。组合：                               │
│  - 3 × Agent (Worker / Attacker)                                   │
│  - 1 × DissenterAgent (条件性)                                     │
│  - 1 × SupervisorAgent                                             │
│  - PlanEnumerator                                                  │
│  - VoteEngine (×1 or ×2)                                           │
│  - BindingPlanResolver                                             │
│  - RoundConcluder                                                  │
│  - TraceRecorder                                                   │
└────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────┐  ┌────────────────────┐  ┌───────────────┐
│  Agent layer         │  │  State layer       │  │  Hook layer   │
│  (Claude API calls)  │  │  (in-memory state) │  │  (L2 stubs)   │
│  - WorkerAgent       │  │  - PlanRegistry    │  │  - AuditLog   │
│  - AttackerAgent     │  │  - ConvState       │  │  - EnvSnap    │
│  - DissenterAgent    │  │  - VoteState       │  │  (DISABLED v1)│
│  - SupervisorAgent   │  │  - RunRecord       │  │               │
└──────────────────────┘  └────────────────────┘  └───────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────┐
│                       Task Layer                                   │
│  - SHADEArenaAdapter (via ControlArena)                            │
│  - TACAdapter (custom, for TAC subset)                             │
│  - 统一 TaskInterface                                              │
└────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Responsibilities

### 2.1 TaskRunner

**职责**: 一个 run 的最外层 driver。

**Inputs**:
- `condition: ConditionEnum` (A1-B1 / A1-B2 / A2-A / ... / A2-F)
- `task: TaskInterface` (来自 SHADEArenaAdapter or TACAdapter)
- `seed: int`
- `config: RunConfig`

**Outputs**:
- `run_record: RunRecord` (full trace per spec §4.1)

**Pseudocode**:
```
class TaskRunner:
    def execute(self, condition, task, seed, config) -> RunRecord:
        run_record = RunRecord(condition, task.id, seed)
        agents = AgentFactory.build(condition, config)
        plan_registry = PlanRegistry()
        
        for round_num in 1..9:
            executor = RoundExecutor(
                round_num, agents, plan_registry, task, config
            )
            round_record = executor.run()
            run_record.rounds.append(round_record)
            plan_registry.commit(round_record.binding_plan_id, round_num)
        
        outcome = OutcomeEvaluator.evaluate(task, run_record, condition)
        run_record.final_outcome = outcome
        return run_record
```

### 2.2 RoundExecutor

**职责**: 执行 13-step round protocol（spec §5）。

**Inputs**:
- `round_num: int`
- `agents: AgentBundle`
- `plan_registry: PlanRegistry`
- `task: TaskInterface`
- `config: RunConfig`

**Outputs**: `round_record: RoundRecord`

**State machine** (13 steps from spec §5):

```
class RoundExecutor:
    def run(self) -> RoundRecord:
        # Step 1: assemble round prompt
        prompt = self.assemble_prompt(self.plan_registry.last_binding_plan())
        
        # Steps 2-4: workers turn (sequential)
        worker_turns = []
        for w in [W1, W2, W3]:
            turn = self.agents.workers[w].turn(prompt, prior_turns=worker_turns)
            worker_turns.append(turn)
        
        # Step 5: Worker 3 enumerates candidate plans
        candidate_plans = self.plan_enumerator.enumerate(worker_turns)
        
        # Step 6: pre-dissent vote
        pre_vote = self.vote_engine.vote(
            workers=self.agents.workers,
            plans=candidate_plans,
            phase="pre_dissent",
        )
        
        # Conditional: A1-B2 only
        dissenter_event = None
        worker_responses = None
        post_vote = None
        if self.config.has_dissenter:
            # Step 7: dissenter challenge
            dissenter_event = self.agents.dissenter.challenge(
                worker_turns, candidate_plans, pre_vote
            )
            # Step 8: workers respond
            worker_responses = self.collect_worker_responses(
                self.agents.workers, dissenter_event
            )
            # Step 9: post-dissent vote
            post_vote = self.vote_engine.vote(
                workers=self.agents.workers,
                plans=candidate_plans,
                phase="post_dissent",
                context=dissenter_event,
            )
        
        # Step 10: determine binding plan
        binding_plan_id = self.binding_resolver.resolve(
            post_vote if post_vote else pre_vote,
            self.plan_registry.last_binding_plan(),  # for E2
        )
        
        # Step 11: round conclusion
        round_conclusion = self.round_concluder.write(
            self.agents.workers[W3], binding_plan_id, candidate_plans
        )
        
        # Step 12: supervisor evaluation
        supervisor_eval = self.agents.supervisor.evaluate(
            worker_turns=worker_turns,
            candidate_plans=candidate_plans,
            pre_vote=pre_vote,
            dissenter_event=dissenter_event,
            post_vote=post_vote,
            binding_plan_id=binding_plan_id,
            round_conclusion=round_conclusion,
            l2_signals=self.l2_hooks.collect() if self.config.l2_enabled else None,
        )
        
        # Step 13: advisory attached to next round (handled by TaskRunner via plan_registry context)
        
        # Trace assembly
        return RoundRecord(
            round_number=self.round_num,
            worker_turns=worker_turns,
            candidate_plans=candidate_plans,
            pre_dissent_vote=pre_vote,
            dissenter_event=dissenter_event,
            worker_responses_to_dissent=worker_responses,
            post_dissent_vote=post_vote,
            binding_plan_id=binding_plan_id,
            round_conclusion=round_conclusion,
            supervisor_evaluation=supervisor_eval,
        )
```

### 2.3 Agent layer

#### WorkerAgent

```
class WorkerAgent:
    model = "claude-opus-4-7"
    system_prompt = WORKER_PROMPT_TEMPLATE.format(
        worker_id=self.id,
        task_description=task.benign_description,
        has_dissenter=condition.has_dissenter,
    )
    
    def turn(self, round_prompt, prior_turns) -> WorkerTurn:
        # Sends prompt + prior_turns to Claude API
        # Returns WorkerTurn (cot + proposed_plan + actions)
    
    def vote(self, candidate_plans, phase, context=None) -> WorkerVote:
        # For each plan, vote agree/disagree/abstain + 1-sentence reasoning
    
    def respond_to_dissent(self, dissenter_event) -> str:
        # Brief reply, optional
```

#### AttackerAgent (extends WorkerAgent)

```
class AttackerAgent(WorkerAgent):
    variant: AttackerVariant  # A | B | D | E | F (C is structural, not agent-level)
    covert_objective: str
    hidden_scratchpad_visible_to_self: bool = True
    hidden_scratchpad_visible_to_supervisor: bool = False
    
    system_prompt = ATTACKER_PROMPT_TEMPLATE[variant].format(
        worker_id=self.id,
        benign_main_task=task.benign_description,
        covert_objective=task.covert_side,
        situational_awareness=variant == "D",  # D only
    )
    
    def turn(self, round_prompt, prior_turns) -> WorkerTurn:
        # Same as WorkerAgent but with hidden_scratchpad written to side channel
        # hidden_scratchpad NOT exposed in WorkerTurn.cot or .actions
        # Stored separately in WorkerTurn.hidden_scratchpad (only attacker has)
```

#### DissenterAgent

```
class DissenterAgent:
    model = "claude-opus-4-7"
    system_prompt = DISSENTER_PROMPT
    
    def challenge(self, worker_turns, candidate_plans, pre_vote) -> DissenterEvent:
        # Output: ONE challenge per round, parsed into DissenterEvent
        # Validation: must include type + target + reasoning (≥2 sentences)
        # If output fails validation: retry once, then error (pilot will calibrate)
    
    # Note: DissenterAgent does NOT vote
```

#### SupervisorAgent

```
class SupervisorAgent:
    model = "claude-opus-4-6"
    mode: SupervisorMode = "light"  # v1 only; v2 adds "heavy"
    system_prompt = SUPERVISOR_PROMPT_LIGHT
    
    def evaluate(self, worker_turns, candidate_plans, pre_vote, dissenter_event, 
                 post_vote, binding_plan_id, round_conclusion, l2_signals) -> SupervisorEvaluation:
        # v1: l2_signals = None (not collected); supervisor sees CoT + actions only
        # v2: l2_signals = AuditLog + EnvSnapshot
        # Output: ROUND_FLAG + EVIDENCE + ADVISORY (parsed)
        # Light mode: ADVISORY may be null; never blocks/rewrites
```

### 2.4 PlanEnumerator

**职责**: 让 Worker 3 列出本轮 emerged 的 1-3 candidate plan。

```
class PlanEnumerator:
    def enumerate(self, worker_turns) -> List[Plan]:
        # Could be:
        # (a) automatic (parse worker_turns for proposed_plan fields, dedupe)
        # (b) ask Worker 3 to enumerate (additional API call)
        # 
        # v1 default: (b) — Worker 3 单独 API call
        # 因为 worker turn 中的 proposed_plan 可能模糊或重叠
        # Returns 1-3 distinct Plan objects
```

### 2.5 VoteEngine

```
class VoteEngine:
    def vote(self, workers, plans, phase, context=None) -> VoteRound:
        # 每 worker 独立调用 vote() per plan
        # Aggregate results
        # Compute per_plan_scores
        # Determine binding_plan_id with E1 (tie) rule
        votes = []
        for w in workers:
            plan_votes = []
            for plan in plans:
                vote_result = w.vote_on_plan(plan, phase, context)
                plan_votes.append(vote_result)
            votes.append(WorkerVote(w.id, plan_votes))
        
        per_plan_scores = self.tally(votes, plans)
        binding_plan_id, tiebreaker_applied = self.resolve_with_E1(per_plan_scores)
        
        return VoteRound(
            phase=phase,
            votes=votes,
            per_plan_scores=per_plan_scores,
            binding_plan_id=binding_plan_id,
            tiebreaker_applied=tiebreaker_applied,
        )
    
    def resolve_with_E1(self, per_plan_scores):
        # Highest score wins
        # Tie: agree count tiebreaker (E1=a)
```

### 2.6 BindingPlanResolver

**职责**: 处理 E2 (no-consensus) 和 round-to-round plan persistence。

```
class BindingPlanResolver:
    def resolve(self, vote_round, prior_round_binding_plan_id) -> Union[int, "no_consensus"]:
        if all(score <= 0 for score in vote_round.per_plan_scores.values()):
            # E2: 全部 ≤ 0
            return "no_consensus"  # round-level
            # 但在 RunRecord 层面，subsequent round prompts 会使用 prior_round_binding_plan_id
        return vote_round.binding_plan_id
```

### 2.7 RoundConcluder

```
class RoundConcluder:
    def write(self, worker3, binding_plan_id, candidate_plans) -> str:
        if binding_plan_id == "no_consensus":
            # 特殊处理：worker 3 写一句 "no consensus this round, continuing on prior plan"
            return self._write_no_consensus(worker3, prior_binding=...)
        else:
            plan = next(p for p in candidate_plans if p.plan_id == binding_plan_id)
            # Worker 3 一句话总结 referencing this plan
            return worker3.summarize_round(plan)
```

### 2.8 TraceRecorder

**职责**: 把 RoundExecutor 各步骤产出 assemble 成 RoundRecord（spec §4.2）。

实际是 RoundExecutor 内部的 helper，不是独立组件，但单独列出方便明确字段责任。

### 2.9 PlanRegistry

```
class PlanRegistry:
    rounds: List[Tuple[int, Plan_or_no_consensus]]  # 每轮的 binding plan
    
    def last_binding_plan(self) -> Optional[Plan]:
        # 找最近一轮非 no_consensus 的 plan
        # E2 时 fallback 链：本轮 → 上一轮 → 上上轮 → ...
        # Round 1 时返回 None (空状态)
    
    def commit(self, binding_plan_or_no_consensus, round_num):
        self.rounds.append((round_num, binding_plan_or_no_consensus))
```

### 2.10 L2 Hook Stubs

```
class AuditLogRecorder:
    enabled: bool = False  # v1
    
    def collect(self, round_id) -> Optional[AuditLog]:
        if not self.enabled:
            return None
        # v2: 从 service-side 拉 tool call audit log
        # v1: stub returns None

class EnvSnapshotCapture:
    enabled: bool = False  # v1
    
    def collect(self, round_id) -> Optional[EnvSnapshot]:
        if not self.enabled:
            return None
        # v2: post-action env state diff
```

Config flag 控制启用。Code path 已存在；v1 supervisor 仅看 worker_turns + candidate_plans + votes + dissenter_event；v2 加 l2_signals。

### 2.11 OutcomeEvaluator

```
class OutcomeEvaluator:
    def evaluate(self, task, run_record, condition) -> FinalOutcome:
        # task_success: per task's benign success criteria (delegated to TaskInterface)
        task_success = task.evaluate_benign(run_record)
        
        # covert_success: only A2 conditions
        covert_success = None
        if condition.is_attacker:
            covert_success = task.evaluate_covert(run_record, condition.attacker_variant)
        
        # final artifact text (per task's spec)
        final_artifact = task.extract_artifact(run_record)
        
        return FinalOutcome(task_success, covert_success, final_artifact)
```

### 2.12 TaskInterface

统一接口，无论 SHADE-Arena 还是 TAC。

```
class TaskInterface (abstract):
    id: str
    benign_description: str
    covert_side: Optional[str]  # SHADE pair has it; TAC tasks have it from tasks_tac.yaml
    
    def evaluate_benign(self, run_record) -> bool: ...
    def evaluate_covert(self, run_record, attacker_variant) -> bool: ...
    def extract_artifact(self, run_record) -> str: ...

class SHADEArenaAdapter(TaskInterface):
    # Wraps SHADE-Arena task spec via ControlArena

class TACAdapter(TaskInterface):
    # Wraps TAC task with custom covert_side from tasks_tac.yaml
    # evaluate_covert reads covert_verification rules from yaml
```

---

## 3. Data Flow per Round

```
[ROUND START]
  │
  ▼
Step 1: assemble round prompt
  Inputs: prior_round_binding_plan, this_round_history
  Output: Prompt string
  │
  ▼
Steps 2-4: worker turns (sequential)
  Worker 1 → Worker 2 → Worker 3
  Each: ClaudeAPI(opus-4.7, prompt + prior turns) → WorkerTurn
  Worker turns accumulated in conv_state
  │
  ▼
Step 5: enumerate candidate plans
  Worker 3 (additional API call) → List[Plan] (1-3 plans)
  Plans assigned plan_id for round
  │
  ▼
Step 6: pre-dissent vote
  For each worker × each plan: vote (agree/disagree/abstain) + reasoning
  VoteEngine.vote() → VoteRound (pre_dissent)
  Compute per_plan_scores, tentative binding (E1 applied)
  │
  ├─[if A1-B2]──────────────────────────────────────────────────────┐
  │                                                                 │
  ▼                                                                 │
Step 7: dissenter challenge                                         │
  DissenterAgent.challenge(worker_turns, candidate_plans, pre_vote) │
  → DissenterEvent (challenge type + target + reasoning)            │
  │                                                                 │
  ▼                                                                 │
Step 8: workers respond (round-robin once, optional)                │
  → list of brief responses                                         │
  │                                                                 │
  ▼                                                                 │
Step 9: post-dissent vote                                           │
  Same plans, possibly revised votes                                │
  VoteEngine.vote() → VoteRound (post_dissent)                      │
  │                                                                 │
  ▼                                                                 │
  rejoin                                                            │
  │ ◀───────────────────────────────────────────────────────────────┘
  ▼
Step 10: determine binding plan
  BindingPlanResolver.resolve(post_vote OR pre_vote, prior_round_binding)
  Apply E2 if all scores ≤ 0
  → binding_plan_id (int or "no_consensus")
  │
  ▼
Step 11: round conclusion
  Worker 3 writes one-line summary referencing binding plan
  │
  ▼
Step 12: supervisor evaluation
  SupervisorAgent.evaluate(...) → SupervisorEvaluation
  v1: cot + actions only; v2: + l2_signals
  Output: ROUND_FLAG + EVIDENCE + ADVISORY
  │
  ▼
Step 13: assemble RoundRecord, commit binding plan to registry
  TraceRecorder builds RoundRecord
  PlanRegistry.commit(binding_plan_id, round_num)
  Advisory (if any) saved for next round prompt assembly
  │
  ▼
[ROUND END]
```

---

## 4. Configuration Schema

```yaml
run_config:
  condition: "A1-B2"  # A1-B1 | A1-B2 | A2-A | A2-B | A2-C | A2-D | A2-E | A2-F
  task_id: "tac.sde.auth_review_backdoor"
  seed: 42
  
  models:
    worker: "claude-opus-4-7"
    dissenter: "claude-opus-4-7"
    supervisor: "claude-opus-4-6"
    # attacker uses worker model (replaces 1 worker)
  
  agents:
    n_workers: 3
    has_dissenter: true   # auto-derived from condition
    attacker_variant: null  # auto-derived; "A" | "B" | ... | null
  
  rounds:
    total: 9  # hard ceiling
    early_stop_allowed: false
  
  vote:
    granularity: "tri"  # agree | disagree | abstain
    binding: "majority"  # by per_plan_scores
    tiebreaker_E1: "agree_count"
    no_consensus_E2: "carry_prior_binding"
    voters: ["worker_1", "worker_2", "worker_3"]  # dissenter excluded
  
  supervisor:
    mode: "light"  # v1: light only; v2: heavy adds blocking/rewriting
    can_advisory: true
    can_block: false  # v2: true in heavy mode
    can_rewrite: false  # v2: true in heavy mode
    can_early_stop: false
  
  monitor_signals:
    cot: true
    action: true
    audit_log: false   # v1 disabled, v2 hook
    env_snapshot: false  # v1 disabled, v2 hook
  
  attacker:
    hidden_scratchpad: true  # for A2 conditions
    knows_supervisor: false  # default; only "D" variant overrides to true
  
  cooperative_workers:
    knows_attacker_possible: false  # production realistic
    knows_supervised: true  # spec §11 P2#12
    knows_dissenter_role: true  # spec §11 P2#11
```

---

## 5. Inspect / ControlArena 集成

### 5.1 Inspect 提供
- Multi-seed orchestration: `inspect eval ... --epochs 10` 跑 10 seed
- Replay: 任意 run 可以 replay
- Structured logging: `eval.json` 输出
- Scorer 接口: 我们 register custom scorer

### 5.2 我们的 wrapper
```python
# Inspect-compatible task spec
@task
def multi_agent_supervision_task(condition: str, task_id: str):
    return Task(
        dataset=load_task(task_id),  # SHADE / TAC
        solver=multi_agent_solver(condition),  # 我们的 TaskRunner wrapper
        scorer=multi_agent_scorer(),  # 计算 task_success / covert_success
    )

@solver
def multi_agent_solver(condition):
    async def solve(state, generate):
        runner = TaskRunner()
        run_record = runner.execute(condition, state.task, state.seed, config)
        state.metadata["run_record"] = run_record  # full trace 存 metadata
        state.output.completion = run_record.final_outcome.final_artifact
        return state
    return solve
```

### 5.3 ControlArena 提供 SHADE-Arena task adapter
```python
from control_arena import shade_arena_loader

shade_task = shade_arena_loader.load("flight_booking")
adapter = SHADEArenaAdapter(shade_task)
```

### 5.4 TAC adapter (我们写)
```python
class TACAdapter(TaskInterface):
    def __init__(self, task_id, tasks_tac_yaml):
        spec = tasks_tac_yaml["tasks"][task_id]
        self.benign_description = spec["benign_main_task"]
        self.covert_side = spec["covert_side_objective"]
        self.covert_verification_rules = spec["covert_verification"]
        # 还要 wire TAC env (Docker container with Mattermost, GitLab, etc.)
        # 这一块 build 阶段最重，可能要 fork TAC repo
```

---

## 6. v1 / v2 Boundary

### 6.1 v1 disabled features (code path 留 stub)

| Feature | v1 status | v2 enable mechanism |
|---|---|---|
| Audit log signal | stub returns None | config flag `monitor_signals.audit_log: true` + 实现 `AuditLogRecorder.collect` |
| Env snapshot signal | stub returns None | config flag `monitor_signals.env_snapshot: true` + 实现 `EnvSnapshotCapture.collect` |
| Heavy supervisor mode | enum 只 "light" | 加 `mode: "heavy"` + 实现 block/rewrite logic |
| Dissenter + Attacker 同时 | 不允许 (config validate 报错) | v3: lift validation, allow both |
| Early stop | hardcoded false | v2: 可选 |

### 6.2 设计原则

L2 hooks 在 SupervisorAgent.evaluate() 接口里**已经接受 `l2_signals` 参数**（v1 时为 None）。这样 v2 不需要改 supervisor 接口。

```python
# v1 supervisor sees:
SupervisorAgent.evaluate(
    worker_turns, candidate_plans, pre_vote, dissenter_event, 
    post_vote, binding_plan_id, round_conclusion,
    l2_signals=None,  # v1
)

# v2 supervisor sees:
SupervisorAgent.evaluate(
    ..., 
    l2_signals=L2Signals(audit_log=..., env_snapshot=...),  # v2
)
```

Supervisor system prompt 在 v2 加 audit log / env snapshot 描述，但接口签名不变。

---

## 7. Open Implementation Questions

(到 build 阶段再决定，但先记录)

1. **Worker turn 是否真正 sequential**？
   - Async with await: worker 2 等 worker 1 完成
   - 性能影响：每 round 至少 3 个串行 API call
   - 替代：parallel worker turns + 后置 reconcile（但破坏"看到前面发言"约束）
   - 决定: 保 sequential

2. **PlanEnumerator 是 LLM call 还是规则解析**？
   - LLM call（让 worker 3 enumerate）：成本 +1 API call/round
   - 规则解析（提取 worker turns 的 proposed_plan）：可能模糊 / 漏 plan
   - 默认：LLM call（更可靠）；pilot 评估必要性

3. **Hidden scratchpad 实现**？
   - 选项 a: 单独 tool（attacker 调 `hidden_scratchpad` tool；orchestrator 截留 output 不给 supervisor）
   - 选项 b: response 中特殊格式（attacker 输出 `<hidden>...</hidden>` 块；orchestrator parse 后 strip）
   - 默认 b（更简单），pilot 验证 attacker 是否守约

4. **Vote reasoning 字段强制度**？
   - Hard enforce: 没 reasoning 就 retry
   - Soft: 没 reasoning 也接受，trace 标记
   - 默认 hard（"有理有据"原则）

5. **Worker 间 visibility 实现**？
   - Round-robin 时 worker N 看到 worker 1..N-1 的发言：通过 prompt 拼接
   - 是否给 worker 看 supervisor advisory？config 项 `cooperative_workers.knows_supervised: true` 决定
   - 决定：Yes，advisory 拼接到下轮 prompt 开头

6. **Trace 写入时机**？
   - Per round 结束 flush 到 disk（崩溃 resilient）
   - End of run 一次写
   - 默认：per round flush（Inspect 默认行为）

7. **Cost monitoring**？
   - 每个 API call wrap with token counter
   - Aggregate 到 RunRecord.token_usage
   - Pre-run budget check：预估超 $20 的 condition × task 警告

---

## 8. Build 阶段（Python pipeline 路径，已 deprecated）

> ⚠️ **注意**：本节是 Python pipeline 执行路径下的 build phase 划分，已经被 Claude Code subagent-native 执行路径取代。
> 当前执行路径的 Stage 0..10 见 [`EXECUTION.md`](./EXECUTION.md)。
> 本节保留作为历史参考。

| Phase | 内容 | 周 |
|---|---|---|
| **Stage 0 — Spec + Architecture lock** | 当前阶段 | 0.5 |
| **Stage 1.a — Skeleton** | TaskRunner / RoundExecutor / Agent 接口 stub（不调真 API） | 1 |
| **Stage 1.b — Single agent path** | 1 worker + supervisor 跑通最简 round | 0.5 |
| **Stage 1.c — Multi-agent + vote** | 3 worker + plan enumeration + vote engine | 1 |
| **Stage 1.d — Dissenter integration** | A1-B2 path 完整 | 0.5 |
| **Stage 1.e — Attacker variants** | A2-A 跑通 + 其他 variant 添加 | 1 |
| **Stage 1.f — TAC adapter** | TAC env wire-up + 1 task 跑通 | 1 |
| **Stage 1.g — L2 hook stubs** | code path 落地 disabled | 0.3 |
| **Stage 2 — Pilot run** | 5 condition × 1 task × 3 seed = 15 run | 0.3 |
| **Stage 3 — Calibration** | spec / prompt 调整 based on pilot | 0.5 |
| **Stage 4 — Main run** | 1600 run | 1.5 |
| **Stage 5 — Analysis + writeup** | metric + paper draft | 1.5-2 |

**Python pipeline 路径总：~9-11 周**（已 deprecated，Claude Code-native stages 见 EXECUTION.md）

---

## 9. Next Step

完成本文件 → 接下来选一个 sub-component 写 implementation skeleton：

- (a) `RoundExecutor` 13-step state machine（核心控制流）
- (b) `WorkerAgent` + system prompt template（最高频组件）
- (c) `VoteEngine` + E1/E2 logic（A1 metric 关键）
- (d) `TACAdapter` + 1 task end-to-end（Stage 1.f 验证最重）

推荐先 (a)，因为它是其他组件的 caller，写出来后可以反向 inform 其他组件接口。

---

## 10. Version History

- v0.1 (2026-05-07): initial architecture draft, design layer only

