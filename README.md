# herdr-orchestrator

基于 Herdr 的本地优先多 harness 工作流控制面。

它让一个确定性 coordinator 持续派发任务给 Droid、Codex、pi、Claude Code、Hermes 等交互式 agent，同时保留 durable queue、lease、重试、去重和收据。可选 planner agent 只负责提出结构化任务，不拥有调度与执行权限。

## 为什么不是让 Herdr 直接当主控

Herdr 提供真实 PTY、detach/reattach、agent 状态、pane 和 workspace 控制，但它不是推理 agent。这个仓库的分工是：

```text
Workflow TOML / Planner task JSON
              ↓
Deterministic coordinator
  queue · lease · retry · dedupe · receipt
              ↓
         Herdr CLI runtime
              ↓
Droid · Codex · pi · Claude Code · Hermes
```

## 前置条件

- Python 3.12+
- Herdr 0.8.2+，并且从 Herdr pane 内运行（`HERDR_ENV=1`）
- 至少一个已登录的 harness CLI
- `just`

## 快速开始

```bash
just doctor
just test

# 把示例任务幂等写入 durable queue
just seed

# 处理当前可运行任务后退出
just run-once

# 持续运行，detach Herdr 后 coordinator 与 agents 继续工作
just run

# 查看任务状态
just status
```

## Workflow-aware harness smoke

下面的命令会依次启动或复用当前 workflow 启用的 harness，并验证每个 agent
都经历真实 turn 后回到 settled state。每个 probe 的 JSON 结果包含所选
workflow 的 canonical path/name/schema version、worker 数量、replica 总容量、
agent/pane identity 和 lifecycle sequence；probe 不会硬编码另一个 workflow：

```bash
just smoke

# 只验证指定 harness，可重复 --harness
just smoke --harness pi --harness claude
```

smoke 不把终端文本当完整 transcript，因为 full-screen agent 的历史可能不进入
Herdr scrollback。验证依据是 agent 成功启动、prompt 被接受并经过 lifecycle
change 后返回 `idle` 或 `done`。请求 workflow 未启用的 harness 会在 dispatch
前失败；临时 pane 会在成功或失败后关闭，已存在并被安全复用的 agent 不会被关闭。

## 添加一个任务

```bash
just enqueue codex review docs/prompts/review.md review-docs-v1
```

参数依次为 `harness`、`title`、`prompt_file`、`dedupe_key`。相同 workflow 下重复的 `dedupe_key` 不会重复入队。

## 工作流

首个示例是 [`workflows/multi-harness.toml`](workflows/multi-harness.toml)。它声明：

- coordinator 的轮询、并发、lease 和重试策略；
- 五个 harness worker；
- 可选 planner agent；
- 可幂等 seed 的示例任务。

配置说明见 [`docs/workflow-schema.md`](docs/workflow-schema.md)，运行与恢复语义见 [`docs/architecture.md`](docs/architecture.md)。

## Schema v2 status、inspect 与 artifact fixture

Schema v2 workflow 使用独立的 executor state，可通过 JSON CLI 查看 durable
run/work/attempt/receipt/artifact/event 真相：

```bash
PYTHONPATH=src python3 -m herdr_orchestrator start \
  --workflow path/to/schema-v2-workflow.toml \
  --dedupe-key example-1 --input "question"
PYTHONPATH=src python3 -m herdr_orchestrator status \
  --workflow path/to/schema-v2-workflow.toml
PYTHONPATH=src python3 -m herdr_orchestrator inspect \
  --workflow path/to/schema-v2-workflow.toml --run-id RUN_ID
PYTHONPATH=src python3 -m herdr_orchestrator run \
  --workflow path/to/schema-v2-workflow.toml --once
PYTHONPATH=src python3 -m herdr_orchestrator artifact-fixture \
  --workflow path/to/schema-v2-workflow.toml --case malformed
```

`artifact-fixture` 是不启动 provider turn 的确定性负向/正向测试入口。每次
调用只使用 disposable workflow state，并返回 `fixture_case_id`、`bound`、
`code`、`reason`、`attempt_consumed`、`attempt_consumption_decision` 以及
work/run state。可用 case 包括 `valid`、`lifecycle-only`、`missing-output`、
`malformed`、`unknown-key`、`oversize`、`wrong-run`、`wrong-work`、
`wrong-attempt`、`wrong-token`、`wrong-digest`、`wrong-input`、
`invalid-lineage`、`absolute-path`、`parent-path`、`path-escape`、
`symlink-escape`、`another-attempt` 和 `unassigned-output`。这些 case 的
`stale` 与 `stale-token` 还覆盖 lease replacement fencing。上述 case 的
domain-negative 结果仍是可解析 JSON，`success` 为 `false`；参数或 selector
错误返回 `success: false` 的结构化错误和非零退出码。

## Research verification fixtures

Research workflows expose a deterministic contradiction and independent
verification seam:

```bash
PYTHONPATH=src python3 -m herdr_orchestrator research verification-fixture \
  --workflow path/to/research-workflow.toml --case critical-independent
PYTHONPATH=src python3 -m herdr_orchestrator research inspect \
  --workflow path/to/research-workflow.toml --run-id RUN_ID
PYTHONPATH=src python3 -m herdr_orchestrator research export \
  --workflow path/to/research-workflow.toml --run-id RUN_ID
```

Canonical cases include `contradiction-pack`, `critical-unverified`,
`critical-independent`, `critical-self-verification`, `critical-downgrade`,
`disposition-history`, `source-reuse`, `unaccounted-contradiction`, and
`stale-assignment`. Fixture runs retain shared-kernel attempts, typed
artifacts, evidence receipts, contradiction history, and terminal
verification state. Repeating a case replays the existing terminal run
without dispatching another attempt. Export writes a Markdown report and
source-claim register below the workspace `.orchestrator/exports` boundary;
contested exports preserve both opposing relations.

## 明确不做

- 不把 `done` 当成质量证明；
- 不自动回答 approval 或 question UI；
- 不自动 push、merge、发布或删除；
- 不把 pane terminal output 当完整 transcript；
- 不让 planner 生成并执行任意 shell command；
- 不在 v1 内做跨机器分布式调度。
