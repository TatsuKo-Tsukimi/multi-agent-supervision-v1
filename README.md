# Multi-Agent Supervision Experiment

**Status**: v0.1 (skeleton + design lock)
**Date**: 2026-05-07

研究"管理员 agent 审 CoT" multi-agent 架构在不同 perturbation（建设性反对者 vs 恶意攻击者）下的行为。基于 SHADE-Arena + TheAgentCompany 任务集，跑 factorial design（worker team perturbation × supervisor 强度）。

## 实验设计概要

两个轴：

- **Plan A**：team 里加 perturbation —— A1 (epistemic dissenter) + A2 (security adversary, 6 attacker variants A-F)
- **Plan B (v2)**：supervisor 强度 —— light (advisory) vs heavy (blocking + multi-signal)
- **Plan C (v2)**：两轴同时
- **Plan D (v2)**：cross-experiment synthesis + writeup

v1 跑 A1 + A2，埋好 L2 接口给 v2。

详见 [`spec.md`](./spec.md)。

## 执行模型 — Claude Code subagent-native

实验**不通过 Anthropic API key 跑**。基于 Claude Max 订阅 + Claude Code subagent 机制：

- 每个角色（Worker / Dissenter / Supervisor / Attacker A-F）= 一个 Claude Code subagent definition (`.claude/agents/*.md`)
- Orchestration 在 main Claude Code session 里通过 Agent tool 调度 subagent
- Trace 记录到 `data/` 目录

详见 [`EXECUTION.md`](./EXECUTION.md)（pending）。

## 文件结构

```
multi_agent_supervision_v1/
├── README.md                   # 你正在看的这个
├── spec.md                     # v1 实验完整 spec (v0.2 lock)
├── architecture.md             # build 阶段 design (v0.1)
├── EXECUTION.md                # 执行模型说明（pending）
├── tasks_tac.yaml              # 15 TAC task with covert side
├── requirements.txt            # Python 依赖
├── .gitignore
├── src/                        # Python 参考实现（架构 reference，非主执行路径）
│   ├── README.md
│   ├── data_models.py          # pydantic trace schema
│   ├── config.py
│   ├── prompts.py              # system prompt 模板（subagent 用）
│   ├── agents.py               # agent class 接口
│   ├── components.py
│   ├── round_executor.py       # 13-step state machine
│   ├── task_runner.py
│   ├── hooks.py                # L2 stubs (v2)
│   └── tasks.py
├── .claude/
│   └── agents/                 # subagent definitions（Worker / Dissenter / Supervisor / Attacker A-F，pending）
├── runners/                    # orchestration scripts (pending)
├── data/                       # run records 输出（gitignored）
└── tests/                      # 单元测试（pending）
```

## 当前进度

| 阶段 | 状态 |
|---|---|
| Spec lock (spec.md v0.2) | ✅ |
| Task suite design (tasks_tac.yaml) | ✅ 15 task drafted, 待 calibrate |
| Architecture design (architecture.md) | ✅ |
| Python 参考 skeleton (src/) | ✅ |
| 文件结构 + git/repo | ✅ |
| Subagent definitions (.claude/agents/) | ⚠️ pending |
| Pilot orchestration script (runners/) | ⚠️ pending |
| ControlArena / TAC env wire-up | ⚠️ pending |
| Pilot run | ⚠️ pending |
| Main run | ⚠️ pending |
| Analysis + writeup | ⚠️ pending |

## 关键决策 log

见 [`spec.md` §0 Decision Log](./spec.md)。

## License

TBD（pending decision before ship）。

## 引用

如果用到这套设计 / data，请引用：

```
TBD pending publication
```
