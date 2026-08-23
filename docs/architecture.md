# 架构与恢复语义

## 目标

首版解决一个问题：在 Herdr 提供的持久 PTY 上，由确定性 coordinator 持续向多个交互式 harness 派发不同类型任务，并在 coordinator 重启后恢复未完成工作。

## 组件

```text
TOML workflow ──┐
Planner JSON ───┼─> Validator ─> SQLite queue ─> Lease scheduler
CLI enqueue ────┘                               │
                                               ▼
                                      Herdr transport adapter
                                               │
                         ┌─────────┬─────────┬──┴────┬────────┐
                         Droid    Codex      pi    Claude   Hermes
```

### Workflow loader

解析 TOML，所有相对路径都相对于 workflow 文件所在目录。未知 harness、重复 worker、越界 timeout 和不存在的 prompt 都 fail closed。

### Durable store

SQLite 是运行状态真源。任务状态为：

```text
pending -> running -> succeeded
                   -> blocked
                   -> pending  (仍可重试)
                   -> failed   (耗尽重试)
```

claim 在 `BEGIN IMMEDIATE` 事务内完成。`running` 任务持有 `lease_until`；coordinator 崩溃后，lease 过期任务可再次 claim。每次 claim 增加 attempt。

### Coordinator

- 一次最多 claim `max_parallel` 个任务；
- provisioning pane 与启动 agent 串行，避免布局竞争；
- agent prompt 可并发等待；
- `idle` / `done` 才成功；
- `blocked` 单独落状态，绝不自动回答；
- `unknown`、timeout 和协议错误按失败与重试策略处理。

### Herdr adapter

- 强制要求 `HERDR_ENV=1`；
- agent 名由 workflow、workspace 与 harness 稳定派生；
- 只复用 kind 与 cwd 都匹配、且处于 settled state 的 agent；
- 每个 harness 使用独立后台 tab 和 full-size root pane，始终 `--no-focus`，避免多 agent 连续 split 后 TUI 过窄；
- 所有 CLI 结果按 JSON schema 读取，不预测 pane ID；
- runtime output 只作为诊断摘要，不作为完整 transcript 或完成证明。

### Planner

planner 是可选输入源，不是调度器。启用后，它只能把以下 JSON 写到配置的 runtime 路径：

```json
{
  "tasks": [
    {
      "title": "任务标题",
      "harness": "codex",
      "prompt": "任务契约",
      "dedupe_key": "稳定去重键"
    }
  ]
}
```

coordinator 校验 harness、字段长度、任务数量和去重键后才入队。JSON 不接受 shell command 字段。

## 24/7 边界

Herdr detach 不终止 coordinator 与 agent 进程，因此适合长时间运行。以下情况不承诺无缝：

- Herdr server 完整重启会终止 pane 进程；
- harness 原生 session 是否可恢复取决于对应 Herdr integration；
- 机器睡眠、重启或网络中断可能让任务 lease 过期后重跑；
- worktree、端口、数据库和 credential 仍可能跨任务共享。

因此任务必须可重试，或者用 `dedupe_key` 与外部系统幂等键保护副作用。默认策略不授权任何外部副作用。
