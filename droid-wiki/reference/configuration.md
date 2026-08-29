# 配置
Active contributors: oldwinter, chendongdong

Durable queue 的主配置是 Workflow TOML；harness 能力目录使用独立的 profile TOML；可选的外部 exporter 只读取环境变量。文档 schema 位于 `docs/workflow-schema.md`，实际 loader 位于 `src/herdr_orchestrator/config.py` 和 `src/herdr_orchestrator/catalog.py`。

## 加载、路径与覆盖顺序

Python runtime 命令先解析 `--workflow`，再由 `load_workflow()` 读取和交叉校验配置。普通相对路径以 workflow 文件所在目录为基准，但 `[placement].worktree_root` 和 `[standardized_delivery]` 下的相对路径以解析后的 workspace 为基准。

| 字段 | 相对路径基准 | 约束 |
| --- | --- | --- |
| `workspace`、`state_db`、`profiles_dir` | Workflow TOML 所在目录 | `workspace` 必须是已有目录 |
| `[planner].prompt_file`、`[planner].output_file` | Workflow TOML 所在目录 | prompt 必须存在；output 必须位于 workspace 内含 `.orchestrator` 的 runtime 路径 |
| `[[seed_jobs]].prompt_file` | Workflow TOML 所在目录 | 必须是已有文件 |
| `[placement].worktree_root` | workspace | 必须位于 workspace 内含 `.orchestrator` 的路径 |
| `[standardized_delivery].tracker_root`、`artifact_root` | workspace | `artifact_root` 必须位于 workspace 内含 `.orchestrator` 的路径 |
| Profile 的 `context_file` | 该 profile TOML 所在目录 | 只接受目录内相对路径；禁止绝对路径和 `..`；文件必须存在 |

Controller 与 worker pool 的覆盖顺序如下：

1. 重复的 `--worker-harness` 整体替换 `[planner].worker_harnesses`；二者都没有时使用全部 `[[workers]]`。
2. `--controller-harness <harness>` 覆盖 `[planner].harness`；显式 `auto` 强制按固定顺序自动选择。
3. 自动 controller 顺序是 `droid → grok → codex → claude → hermes → pi`，并要求候选 CLI 在本机可执行。
4. 显式 controller 可以不在 worker pool 中，但必须有 profile；单任务 `enqueue --harness` 仍必须落在有效 worker pool。

Placement 的优先级是：任务显式值、worker 默认值、全局固定 mode、`hybrid` 确定性语义规则、最后才是 controller 的严格 topology JSON。详情见 [Placement 与 worktree](../primitives/placement-and-worktrees.md)。

## 顶层与 coordinator

| 字段 | 必需 | 类型或范围 | 默认值 |
| --- | --- | --- | --- |
| `schema_version` | 是 | integer，当前只能是 `1`；bool 不算 integer | 无 |
| `name` | 是 | `[a-z][a-z0-9_-]{0,63}` | 无 |
| `workspace` | 是 | 非空 path string，最长 4096 | 无 |
| `state_db` | 是 | 非空 path string，最长 4096 | 无 |
| `profiles_dir` | 否 | 非空 path string，最长 4096 | `../profiles/harnesses` |

`[coordinator]` 必须存在，且以下字段都必须显式给出：

| 字段 | 范围 | 语义 |
| --- | --- | --- |
| `poll_seconds` | 1–3600 | 空队列轮询间隔 |
| `max_parallel` | 1–16 | 单个 wave 的最大 claim 数 |
| `lease_seconds` | 30–86400 | running lease |
| `max_attempts` | 1–10 | 新 job 的 attempt budget |
| `agent_timeout_seconds` | 10–3600 | topology、启动、prompt、settlement 和 receipt 验证的完整 deadline |

跨字段约束是 `lease_seconds >= agent_timeout_seconds + 90`。否则旧 turn 尚未结束时 lease 可能被重复 claim，loader 会以 `lease_seconds_must_cover_agent_timeout` 拒绝配置。

## Placement

`[placement]` 可省略。

| 字段 | 允许值 | 默认值 |
| --- | --- | --- |
| `mode` | `hybrid`、`tab`、`pane`、`worktree` | `hybrid` |
| `worktree_root` | workspace runtime 内的 path | `.orchestrator/worktrees` |

`hybrid` 允许 job 先以 `placement = NULL` 入库，待规则或 controller 决定后再 claim。非 Git workspace 不接受 `worktree`；CLI 的 `--placement auto` 与 worker/seed 的 `placement="auto"` 表示“不覆盖”，不是持久化枚举值。

## Worker 与 planner

`[[workers]]` 至少一项。支持的 harness 只有 `droid`、`grok`、`codex`、`pi`、`claude`、`hermes`。

| Worker 字段 | 必需 | 范围 | 默认值 |
| --- | --- | --- | --- |
| `name` | 是 | `[a-z][a-z0-9_-]{0,31}`，全 workflow 唯一 | 无 |
| `harness` | 是 | 六个支持值之一；同一 harness 只能声明一次 | 无 |
| `capabilities` | 否 | 最多 32 个非空 string | `[]` |
| `replicas` | 否 | 1–16 | `1` |
| `placement` | 否 | `auto`、`tab`、`pane`、`worktree` | `auto` |

同 harness 并发只能通过 `replicas` 增加。`capabilities` 是兼容字段，controller 使用的能力描述真源仍是 `profiles/harnesses/*.toml`。

`[planner]` 在当前 loader 中必须存在；`enabled=false` 也不会跳过字段校验。

| Planner 字段 | 必需 | 范围 | 默认语义 |
| --- | --- | --- | --- |
| `enabled` | 是 | boolean | 无 |
| `harness` | 否 | `auto` 或支持的 harness | `auto`，内存中为 `None` |
| `worker_harnesses` | 否 | 非空、无重复的 harness array | 省略时使用全部 worker |
| `interval_seconds` | 是 | 60–86400 | 无 |
| `prompt_file` | 是 | 已有文件 | 无 |
| `output_file` | 是 | workspace `.orchestrator` 内的 JSON 路径 | 无 |
| `max_tasks` | 是 | 1–100 | 无 |

Planner 只能把受 schema 校验的 task JSON 写到 `output_file`，不能提交 shell 命令。其候选 harness 必须有 worker；显式 planner/controller harness 必须有 catalog profile。

## 标准化交付

`[standardized_delivery]` 可省略；只有显式执行 `deliver` 才会使用它。CLI 上同名参数逐字段覆盖 TOML。

| 字段 | 范围 | 默认值 |
| --- | --- | --- |
| `tracker_backend` | `local-markdown`、`github` | `local-markdown` |
| `tracker_root` | path | `.scratch/standardized-delivery` |
| `artifact_root` | workspace runtime 内的 path | `.orchestrator/deliveries` |
| `github_repository` | 非空 string，最长 200 | 无 |
| `wayfinder` | `auto`、`always`、`never` | `auto` |
| `max_parallel` | 1–3 | `3` |
| `review_repair_rounds` | 0–2 | `2` |

`tracker_backend="github"` 要求 `github_repository`；tracker 实例化还会校验 `owner/repo`。GitHub backend 会创建、更新和关闭本次交付的 issues，因此是 opt-in 外部写操作。完整 artifact 关系见[交付 Artifact](../primitives/delivery-artifacts.md)。

## Seed job

`[[seed_jobs]]` 可省略。`seed` 依靠 SQLite 的 `(workflow, dedupe_key)` 唯一约束幂等写入。

| 字段 | 必需 | 约束 |
| --- | --- | --- |
| `title` | 是 | 非空 string，最长 200 |
| `harness` | 是 | 必须对应已声明 worker |
| `prompt_file` | 是 | 已有文件 |
| `dedupe_key` | 是 | `[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}`，本 workflow 内唯一 |
| `placement` | 否 | `auto`、`tab`、`pane`、`worktree` |

Seed job 当前不能在 TOML 中声明 task receipt；receipt 是 `enqueue` 的 CLI 契约。见[任务与收据](../primitives/jobs-and-receipts.md)。

## Harness profile

每个 `profiles/harnesses/<harness>.toml` 的 key 集必须精确等于：

`schema_version`、`harness`、`display_name`、`summary`、`strengths`、`best_for`、`avoid_for`、`traits`、`context_file`。

`src/herdr_orchestrator/catalog.py` 会拒绝未知 key、重复 harness、空列表、越界文本和不安全 context 路径。Compact catalog 不含 Markdown context；只有选中 harness 后才加载 `context_file`，去空白后必须非空且不超过 50,000 字符。详见 [Harness profile](../primitives/harness-profiles.md)。

## 可选 exporter 环境变量

`.env.example` 是模板，runtime 不会自动加载 `.env`；这些值必须进入进程环境。`src/herdr_orchestrator/feature_flags.py` 只接受 `1/true/yes/on` 与 `0/false/no/off/空字符串`，不合法值会失败关闭。

| 环境变量 | `.env.example` 值 | 作用 |
| --- | --- | --- |
| `HERDR_FEATURE_SENTRY_EXPORT` | `false` | 显式启用 Sentry error export |
| `SENTRY_DSN` | 空 | Sentry DSN；仅接受实现所需的 HTTPS 形状 |
| `HERDR_FEATURE_POSTHOG_ANALYTICS` | `false` | 显式启用 PostHog event export |
| `POSTHOG_API_KEY` | 空 | PostHog project key |
| `POSTHOG_HOST` | `https://us.i.posthog.com` | 必须是 HTTPS host |
| `HERDR_FEATURE_WEBHOOK_ALERTS` | `false` | 显式启用 alert webhook |
| `HERDR_ALERT_WEBHOOK_URL` | 空 | 必须是 HTTPS URL |
| `HERDR_RELEASE` | `development` | 写入导出 error event 的 release 标签；不得放凭据 |

所有 feature flag 默认关闭；key、DSN 或 HTTPS URL 缺失时不会发送。隐私与本地 JSONL 行为见[可观测性与 Attention](../features/observability-and-attention.md)。

## 关键源文件

| 完整路径 | 用途 |
| --- | --- |
| `docs/workflow-schema.md` | Schema v1 文档 |
| `src/herdr_orchestrator/config.py` | Workflow 字段、默认值、路径与交叉校验 |
| `src/herdr_orchestrator/catalog.py` | Profile schema 与 context 延迟加载 |
| `src/herdr_orchestrator/model.py` | 解析后的配置 dataclass 与 enum |
| `workflows/multi-harness.toml` | 六 harness durable queue 示例 |
| `workflows/grok-research.toml` | 单 harness、多 replica planner 示例 |
| `profiles/harnesses/` | Compact profile 与完整 context 真源 |
| `.env.example` | 可选 exporter 环境变量模板 |
