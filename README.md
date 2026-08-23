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

## 五 harness 真实只读 smoke

下面的命令会依次启动或复用五种 harness，要求它们只读检查两个本地配置文件，并验证五个 agent 都经历真实 turn 后回到 settled state：

```bash
just smoke

# 只验证指定 harness，可重复 --harness
just smoke --harness pi --harness claude
```

smoke 不把终端文本当完整 transcript，因为 full-screen agent 的历史可能不进入 Herdr scrollback。验证依据是 agent 成功启动、prompt 被接受并经过 lifecycle change 后返回 `idle` 或 `done`。临时 pane 会在成功或失败后关闭；已存在并被安全复用的 agent 不会被关闭。

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

## 明确不做

- 不把 `done` 当成质量证明；
- 不自动回答 approval 或 question UI；
- 不自动 push、merge、发布或删除；
- 不把 pane terminal output 当完整 transcript；
- 不让 planner 生成并执行任意 shell command；
- 不在 v1 内做跨机器分布式调度。
