# Workflow TOML schema

## 顶层

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | integer | 当前必须为 `1` |
| `name` | string | `[a-z][a-z0-9_-]{0,63}` |
| `workspace` | path | worker 的 cwd，相对 workflow 文件 |
| `state_db` | path | SQLite runtime state，相对 workflow 文件 |
| `profiles_dir` | path | harness catalog 目录，相对 workflow 文件 |

## `[coordinator]`

| 字段 | 默认边界 | 说明 |
| --- | --- | --- |
| `poll_seconds` | 1–3600 | 队列空闲轮询间隔 |
| `max_parallel` | 1–16 | 单轮最大并发 |
| `lease_seconds` | 30–86400 | running lease |
| `max_attempts` | 1–10 | 总 claim 次数 |
| `agent_timeout_seconds` | 10–3600 | 单次完整派发 deadline，包含 topology provisioning、agent 启动、prompt、settlement 与 receipt 验证 |

`lease_seconds` 必须至少比 `agent_timeout_seconds` 长 90 秒，为 agent 启动、Herdr 控制请求和收据提交留出窗口，防止同一任务在旧 turn 尚未结束时被重复 claim。

## `[placement]`

该 table 可省略。它决定普通 durable queue 如何把任务映射到 Herdr 拓扑：

```toml
[placement]
mode = "hybrid"
worktree_root = ".orchestrator/worktrees"
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `mode` | `hybrid` | `hybrid`、`tab`、`pane` 或 `worktree` |
| `worktree_root` | `.orchestrator/worktrees` | 原生 worktree checkout 根目录，必须位于 workspace 的 `.orchestrator` |

`hybrid` 的决策顺序是：

1. enqueue/seed 的显式 `placement`；
2. worker 的默认 `placement`；
3. 确定性语义规则：明确只读任务用 `pane`，明确仓库写任务用 `worktree`；
4. 仍模糊时，controller 只写严格
   `{"placement":"tab|pane|worktree","rationale":"..."}` JSON；
5. coordinator 校验 Git 能力与枚举后才写入 queue。

非 Git workspace 不接受 `worktree`。选择 `pane` 时，同一次 `run_once` 的任务共享一个
短标题 tab，但每个 agent 仍占独立 pane。选择 `worktree` 时使用 Herdr 原生
`worktree create/open`，并保留 workspace、checkout 和 branch。

## `[[workers]]`

每个 worker 需要唯一 `name` 和受支持的 `harness`。同一 harness 只能声明一个 worker；并行靠可选 `replicas`（1–16，默认 1）开多个同 kind pane。可选 `capabilities` 为兼容字段，主控使用的能力描述真源是 `profiles_dir` 下同名 profile。v1 支持：

`droid`、`grok`、`codex`、`pi`、`claude`、`hermes`

Herdr 0.8.2 还支持更多 kind，但必须先加入代码白名单和测试，才能进入此仓库的稳定面。

worker 可选 `placement = "tab"|"pane"|"worktree"` 作为该 harness 的 topology 默认值；
省略时由 `[placement]` 决定。`replicas` 始终限制同 harness 总并发，不因 topology
种类增加。

## Harness profile

每个 `<harness>.toml` 是主控预加载的 compact metadata：

- `schema_version`
- `harness`
- `display_name`
- `summary`
- `strengths`
- `best_for`
- `avoid_for`
- `traits`
- `context_file`

`context_file` 必须是 profile 目录内的相对 Markdown 路径。完整 Markdown 只在该 harness 被选中并 dispatch 时读取。

## `[planner]`

planner 默认建议关闭，明确配置 `enabled = true` 才会运行。

| 字段 | 说明 |
| --- | --- |
| `harness` | planner/router 使用的主控 harness；可省略或设为 `auto` |
| `worker_harnesses` | 可选的 worker 候选列表；省略时使用全部 `[[workers]]` |
| `interval_seconds` | 两次 planner turn 的最短间隔 |
| `prompt_file` | planner 基础约束 |
| `output_file` | planner 唯一允许写入的 runtime JSON |
| `max_tasks` | 单次最多接收任务数，1–100 |

planner 的 `output_file` 必须位于 workflow workspace 内或其 `.orchestrator` runtime 路径，且不能指向已跟踪的 prompt 或源码。

`harness = "auto"` 时，coordinator 按 `droid → grok → codex → claude → hermes → pi` 的固定优先级，从候选 worker 中选择本机 executable 存在的 harness。显式主控可以不在 worker 候选池中，但必须有 catalog profile 和可用 CLI。

`run` 与 `enqueue` 可用 `--controller-harness` 和可重复的 `--worker-harness` 临时覆盖这些默认值。`enqueue --harness auto` 或省略 `--harness` 时，主控读取候选池 compact catalog 并输出严格的单 harness JSON；显式 `--harness` 则跳过这次路由 turn。

Task receipt 是 CLI enqueue 契约，不是 TOML workflow 字段。`--receipt-prefix` 要求 agent
detection output 中有一行以指定值开头；`--receipt-file` 要求 execution root 下相对路径为
非空文件。二者互斥，缺失时任务不能成功。`[[seed_jobs]]` 暂不声明 task receipt。

## `[standardized_delivery]`

该 table 可省略；`deliver` command 本身就是 opt-in gate。默认值：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `tracker_backend` | `local-markdown` | `local-markdown` 或 `github` |
| `tracker_root` | `.scratch/standardized-delivery` | local Markdown spec/ticket 根目录，相对 workspace |
| `artifact_root` | `.orchestrator/deliveries` | runtime artifact/worktree 根目录，必须位于 workspace 的 `.orchestrator` |
| `github_repository` | 无 | GitHub backend 必填，格式 `owner/repo` |
| `wayfinder` | `auto` | `auto`、`always` 或 `never` |
| `max_parallel` | `3` | 同一 frontier 最大并发，1–3 |
| `review_repair_rounds` | `2` | accepted must-fix finding 的最大修复轮数，0–2 |

`deliver` 接受同名 CLI override，以及 `--controller-harness` 和可重复
`--worker-harness`。controller 选择和 worker compact catalog 仍遵循 `[planner]`
及 runtime override。

```toml
[standardized_delivery]
tracker_backend = "local-markdown"
tracker_root = ".scratch/standardized-delivery"
artifact_root = ".orchestrator/deliveries"
wayfinder = "auto"
max_parallel = 3
review_repair_rounds = 2
```

`github` backend 会创建 spec issue、ticket issues，并在 receipt 验收后更新和关闭
ticket。该 backend 是明确的外部写操作，仅应在用户选择它时使用。

## `[[seed_jobs]]`

seed job 用于可复现的固定工作包：

- `title`
- `harness`
- `prompt_file`
- `dedupe_key`
- 可选 `placement = "tab"|"pane"|"worktree"`

`seed` 可重复执行；同一 workflow 的 `dedupe_key` 唯一。
