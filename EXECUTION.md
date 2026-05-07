# Execution Model — Claude Code subagent-native

**Status**: v0.1 design (pending implementation)
**Date**: 2026-05-07

## Why not use Anthropic API key?

[Anthropic 政策](https://github.com/anthropics/claude-agent-sdk-python/issues/559)（2026-04 起）明确：
- Pro/Max OAuth subscription 不支持 programmatic SDK billing
- Agent SDK programmatic 调用必须用 API key
- 第三方 wrapper（claude_max 等）已经被 cut off

→ 用户已有 Claude Max 订阅但不希望走 API key 现金支付。**唯一合规路径：让实验本身跑在 Claude Code 内部，作为 interactive session 的扩展**。

## 执行模型

```
┌─────────────────────────────────────────────────────┐
│ Main Claude Code Session (orchestrator)             │
│  - 加载 spec + task config                          │
│  - 跑 13-step round protocol                        │
│  - 通过 Agent tool 调度 subagent                    │
│  - 收集 trace 写入 data/                            │
└─────────────────────────────────────────────────────┘
                  │
                  ├─── Agent(worker-1) ──► subagent
                  ├─── Agent(worker-2) ──► subagent
                  ├─── Agent(worker-3) ──► subagent
                  ├─── Agent(dissenter) ─► subagent (A1-B2 only)
                  └─── Agent(supervisor) ► subagent
```

每个 role = 一个 `.claude/agents/{role}.md` subagent definition。

## Subagent definitions (待写)

待落地的 subagent 文件：

- `.claude/agents/worker.md` — cooperative worker prompt
- `.claude/agents/dissenter.md` — constructive dissenter prompt
- `.claude/agents/supervisor.md` — light supervisor prompt
- `.claude/agents/attacker-a.md` ~ `.claude/agents/attacker-f.md` — 6 attacker variants

每个 subagent 文件 frontmatter 需要：
- `description`: subagent 的用途（决定 main session 何时调用）
- `tools`: 该 role 允许使用的 tool 集合
- 系统 prompt 内容（来自 `src/prompts.py` 的对应模板）

## Orchestration script

待写：`runners/run_pilot.py` 或 `runners/run_pilot.md`（Markdown-driven 流程脚本，让 Claude Code 跟着执行）

形态待定：

- **Option A**：Python 脚本通过 `claude -p --resume` subprocess 模拟，但这个还是 subprocess 路径，跟 native subagent 不同
- **Option B**：Markdown-driven runbook，main Claude Code session 跟着 runbook 一步一步执行，调 Agent tool 跑每个 round
- **Option C**：写一个 slash command / skill，触发后自动跑 round protocol

**Option B 是最贴 Claude Code-native 的**。Pilot 阶段先用这个验证。

## Trace 收集

- `data/runs/{run_id}/round_{N}.json` — 每 round 的 RoundRecord
- `data/runs/{run_id}/run.json` — 完整 RunRecord
- `data/runs/{run_id}/trace.md` — 人可读的完整 transcript

`data/` 在 .gitignore 里（runs 数据可能含 sensitive task content）。

## 已知挑战

1. **Quota 占用大**：每 `claude -p` / Agent 调用 fresh 都付 ~13K cache creation tokens（CC 框架 overhead）。1 run ≈ $9-27 equivalent，1600 run 在 Max plan 上跑不完。
2. **Subagent 输出 parsing**：subagent 返回的是 summary 而不是 raw response，对结构化输出（vote JSON / dissent format）可能不友好。需要 pilot 验证。
3. **Trace 持久化**：subagent 内部的 CoT 默认不暴露。需要在 subagent prompt 里 explicit 要求结构化输出 + 主 session 解析后落盘。
4. **Round-to-round 状态**：subagent 是 stateless 的（每次调用独立 context）。需要主 session 把 prior round 的 binding plan / advisory 拼进 round prompt。

## Pilot 计划

**最小 pilot**：
- 1 task（mock，不依赖 SHADE / TAC env）
- 1 condition（A1-B1：cooperative，最简）
- 1 round（不是 9，先验证 round protocol 能跑通）
- 估算 quota 占用 → decide 是否扩

**扩展 pilot**：
- 1 task × 5 conditions × 1 seed × 9 round = 45 round = ~450 agent call
- 估算成本 $50-150 equivalent quota
- Max plan 5h 窗口可能跑 1-2 次

## v1 Build Phase（针对 Claude Code-native execution 重排）

| Phase | 内容 | 估时 |
|---|---|---|
| **B0** Spec + architecture lock | ✅ done |
| **B1.a** File org + repo setup | ✅ done |
| **B1.b** Subagent definitions（9 个 .md 文件） | 2-3 小时 |
| **B1.c** Mock task design + verifier | 1-2 小时 |
| **B1.d** Orchestration runbook (B option, Markdown-driven) | 2-3 小时 |
| **B1.e** Trace 收集 helper（Python，分析 用） | 1 小时 |
| **B2** Minimal pilot (1 round, A1-B1) | 0.5 小时（runtime 短） |
| **B3** Calibration based on pilot | 1-2 小时 |
| **B4** 扩展 pilot（5 conditions × 1 task × 1 seed × 9 round） | 半天到一天（quota window 限制） |
| **B5** Full v1 main run | 视 quota 情况 staged |
| **B6** Analysis + writeup | 1-2 周 |

→ Build 总时长 1-2 周（excluding main run），main run 受 Max quota 制约只能慢慢跑。
