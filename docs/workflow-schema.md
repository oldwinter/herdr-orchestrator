# Workflow TOML schema

## 顶层

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | integer | 当前必须为 `1` |
| `name` | string | `[a-z][a-z0-9_-]{0,63}` |
| `workspace` | path | worker 的 cwd，相对 workflow 文件 |
| `state_db` | path | SQLite runtime state，相对 workflow 文件 |

## `[coordinator]`

| 字段 | 默认边界 | 说明 |
| --- | --- | --- |
| `poll_seconds` | 1–3600 | 队列空闲轮询间隔 |
| `max_parallel` | 1–16 | 单轮最大并发 |
| `lease_seconds` | 30–86400 | running lease |
| `max_attempts` | 1–10 | 总 claim 次数 |
| `agent_timeout_seconds` | 10–3600 | 单个 agent prompt wait |

`lease_seconds` 必须至少比 `agent_timeout_seconds` 长 90 秒，为 agent 启动、Herdr 控制请求和收据提交留出窗口，防止同一任务在旧 turn 尚未结束时被重复 claim。

## `[[workers]]`

每个 worker 需要唯一 `name`、受支持的 `harness` 和可选 `capabilities`。v1 支持：

`droid`、`codex`、`pi`、`claude`、`hermes`

Herdr 0.8.2 还支持更多 kind，但必须先加入代码白名单和测试，才能进入此仓库的稳定面。

## `[planner]`

planner 默认建议关闭，明确配置 `enabled = true` 才会运行。

| 字段 | 说明 |
| --- | --- |
| `harness` | planner 使用的 harness |
| `interval_seconds` | 两次 planner turn 的最短间隔 |
| `prompt_file` | planner 基础约束 |
| `output_file` | planner 唯一允许写入的 runtime JSON |
| `max_tasks` | 单次最多接收任务数，1–100 |

planner 的 `output_file` 必须位于 workflow workspace 内或其 `.orchestrator` runtime 路径，且不能指向已跟踪的 prompt 或源码。

## `[[seed_jobs]]`

seed job 用于可复现的固定工作包：

- `title`
- `harness`
- `prompt_file`
- `dedupe_key`

`seed` 可重复执行；同一 workflow 的 `dedupe_key` 唯一。
