# Workflow TOML schema

## 顶层

| 字段 | 必需 | 类型与范围 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `schema_version` | 是 | integer，只能是 `1` | 无 | Workflow schema 版本 |
| `name` | 是 | `[a-z][a-z0-9_-]{0,63}` | 无 | Workflow 标识，也是 queue scope |
| `workspace` | 是 | 非空 path string，最长 4096 字符 | 无 | Worker cwd；必须是已有目录 |
| `state_db` | 是 | 非空 path string，最长 4096 字符 | 无 | SQLite runtime state；父目录可在初始化时创建 |
| `profiles_dir` | 否 | 非空 path string，最长 4096 字符 | `../profiles/harnesses` | Harness catalog 目录 |

相对路径按 workflow TOML 所在目录解析；这三个字段也接受绝对路径。

## 路径权限边界

Workflow TOML 是本地控制面的可信输入。调用方必须先确认 workflow 文件及其引用的路径。
为兼容已有共享运行状态和 tracker 部署，`state_db` 和 `tracker_root` 可以解析到 workspace
外部；loader 只做规范化，不把这两个路径当作安全沙箱。

`planner.prompt_file` 必须解析到 workspace 内，但可以使用 workspace 内的绝对路径。
`placement.worktree_root`、`[planner].output_file` 和 `[standardized_delivery].artifact_root`
必须解析到 workspace 内且路径组成中包含 `.orchestrator`。这些执行产物路径在加载时
fail closed。这个差异保留了共享状态的向后兼容，同时限制了 worker 产物位置。

Planner 输出校验只约束 coordinator 接受的 task JSON。`command`、`argv` 等字段会被拒绝，
coordinator 不执行模型提交的 shell。这个校验不是进程或工具沙箱。planner harness 仍按所选
harness 的最高自动化参数运行，六种 harness 没有共同的可移植 no-tools 模式；被攻陷的
harness 仍可能使用自身工具。prompt policy 不能改变这一点。worktree 只隔离 checkout，
不是安全沙箱。

## `[coordinator]`

该 table 必需，且所有字段都必需。

| 字段 | 类型与范围 | 说明 |
| --- | --- | --- |
| `poll_seconds` | integer，1–3600 | 队列空闲轮询间隔 |
| `max_parallel` | integer，1–16 | 单轮最大并发 |
| `lease_seconds` | integer，30–86400 | running lease |
| `max_attempts` | integer，1–10 | 总 claim 次数 |
| `agent_timeout_seconds` | integer，10–86400 | 单次完整派发 deadline，包含 topology provisioning、agent 启动、prompt、settlement 与 receipt 验证。到期后 coordinator 停止等待并记 `herdr_timeout`，不会杀死已在跑的 Herdr agent；lease 过期后同一任务可能被重新 claim |
| `readiness_ttl_seconds` | integer，1–604800 | readiness 成功证据有效期，默认 `3600` |
| `readiness_cooldown_seconds` | integer，1–86400 | degraded/unavailable 后再次 refresh 前的 cooldown，默认 `300` |
| `readiness_probe_timeout_seconds` | integer，5–300 | bounded readiness refresh timeout，默认 `30` |

`lease_seconds` 必须至少比 `agent_timeout_seconds` 长 90 秒，为 agent 启动、Herdr 控制请求和收据提交留出窗口，防止同一任务在旧 turn 尚未结束时被重复 claim。

健康字段也可以放在可选的 `[harness_health]` table 中，使用 `ttl_seconds`、`cooldown_seconds`
和 `probe_timeout_seconds` 短名称；`[readiness]` 是兼容别名。所有配置都保持相同范围，省略
时使用上面的默认值。现有 workflow 不需要新增字段。

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
| `worktree_root` | `.orchestrator/worktrees` | 原生 worktree checkout 根目录；解析后的路径必须在 workspace 内，且路径组成中包含 `.orchestrator` |

`hybrid` 的决策顺序是：

1. enqueue/seed 的显式 `placement`；
2. worker 的默认 `placement`；
3. `[placement].mode`：如果不是 `hybrid`，直接使用该固定模式；
4. `hybrid` 下的确定性语义规则：明确只读任务用 `pane`，明确仓库写任务用
   `worktree`，非 Git workspace 退回 `tab`；
5. 仍模糊时，controller 只写严格的
   `{"placement":"pane","rationale":"..."}` JSON，其中 `placement` 必须是一个实际枚举值；
6. coordinator 校验 Git 能力与枚举后才写入 queue。

非 Git workspace 不接受 `worktree`。选择 `pane` 时，同一次 `run_once` 的任务共享一个
短标题 tab，但每个 agent 仍占独立 pane。选择 `worktree` 时使用 Herdr 原生
`worktree create/open`，并保留 workspace、checkout 和 branch。

## `[[workers]]`

至少需要一个 worker。每个 worker 需要唯一 `name` 和受支持的 `harness`。同一 harness 只能声明一个 worker；并行靠可选 `replicas`（1–16，默认 1）开多个同 harness slot，具体 topology 由 placement 决定。主控使用的能力描述真源是 `profiles_dir` 下同名 profile。v1 支持：

`droid`、`grok`、`codex`、`pi`、`claude`、`hermes`

Herdr 0.8.2 还支持更多 kind，但必须先加入代码白名单和测试，才能进入此仓库的稳定面。

| 字段 | 必需 | 类型与范围 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | 是 | `[a-z][a-z0-9_-]{0,31}` | 无 | Worker 配置名 |
| `harness` | 是 | 六个受支持值之一 | 无 | 同一 harness 只能声明一次 |
| `capabilities` | 否 | string array，最多 32 项，每项非空 | `[]` | 兼容字段 |
| `replicas` | 否 | integer，1–16 | `1` | 该 harness 的总并发 slot 数 |
| `placement` | 否 | `auto`、`tab`、`pane`、`worktree` | `auto` | 该 harness 的 topology 默认值 |

省略 worker 的 `placement` 时由 `[placement]` 决定。`replicas` 始终限制同 harness 总并发，不因 topology 种类增加。

## Harness profile

每个 `<harness>.toml` 是主控预加载的 compact metadata。字段约束如下：

| 字段 | 约束 |
| --- | --- |
| `schema_version` | integer，只能是 `1` |
| `harness` | 六个受支持 harness 之一；整个目录内唯一 |
| `display_name` | 非空 string，最长 80 |
| `summary` | 非空 string，最长 300 |
| `strengths` | 1–12 个非空 string，每项最长 120 |
| `best_for` | 1–12 个非空 string，每项最长 160 |
| `avoid_for` | 1–12 个非空 string，每项最长 160 |
| `traits` | 1–12 个非空 string，每项最长 160 |
| `context_file` | profile 目录内相对 path，最长 255，目标文件必须存在 |

Profile 只接受声明表中的字段。`context_file` 内容只在该 harness 被选中并 dispatch 时读取；去除首尾空白后必须非空且不超过 50,000 字符。

## `[planner]`

该 table 必需。`enabled = false` 时字段仍要通过完整加载校验。

| 字段 | 必需 | 类型与范围 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `enabled` | 是 | boolean | 无 | 是否运行 planner |
| `harness` | 否 | `auto` 或六个 harness 之一 | `auto` | Planner/router 使用的主控 harness |
| `worker_harnesses` | 否 | 非空、无重复的 harness array | 省略时使用全部 worker | Controller 可选择的 worker pool |
| `interval_seconds` | 是 | integer，60–86400 | 无 | 两次 planner turn 的最短间隔 |
| `prompt_file` | 是 | 已有非空 path string，最长 4096 字符，解析后必须在 workspace 内 | 无 | Planner 基础约束 |
| `output_file` | 是 | 非空 path string，最长 4096 字符 | 无 | Planner 唯一 runtime JSON 输出 |
| `max_tasks` | 是 | integer，1–100 | 无 | 单次最多接收任务数 |

planner 的 `output_file` 必须位于 workflow workspace 内，且解析后的路径组成中必须包含 `.orchestrator`。

`harness = "auto"` 时，coordinator 按 `droid → grok → codex → claude → hermes → pi` 的固定优先级，
只从有 fresh `ready` health evidence 的候选 worker 中选择。unknown/过期候选只做一次 bounded
refresh；没有可用候选时 fail closed 并报告每个 harness 的 reason。显式主控可以不在 worker 候选池中，
但必须通过同一 bounded preflight，不会静默切换到另一个 harness。

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
| `artifact_root` | `.orchestrator/deliveries` | runtime artifact/worktree 根目录；解析后的路径必须在 workspace 内，且路径组成中包含 `.orchestrator` |
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

seed job 用于可复现的固定工作包。该 array of tables 可完全省略。

| 字段 | 必需 | 类型与范围 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `title` | 是 | 非空 string，最长 200 字符 | 无 | Job 标题 |
| `harness` | 是 | 已声明 worker 的 harness | 无 | 固定 worker |
| `prompt_file` | 是 | 已有非空 path string，最长 4096 字符 | 无 | Seed prompt，相对 workflow 文件 |
| `dedupe_key` | 是 | `[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}` | 无 | Workflow 内唯一 |
| `placement` | 否 | `auto`、`tab`、`pane`、`worktree` | `auto` | 单任务 topology override |

`seed` 可重复执行；同一 workflow 的 `dedupe_key` 唯一。
