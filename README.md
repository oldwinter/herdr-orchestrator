# herdr-orchestrator

基于 Herdr 的本地优先多 harness 工作流控制面。

它让一个确定性 coordinator 持续派发任务给 Droid、Grok Build、Codex、pi、Claude Code、Hermes 等交互式 agent，同时保留 durable queue、lease、重试、去重和收据。可选 planner agent 只负责提出结构化任务，不拥有调度与执行权限。

## Harness catalog，像 Skills 一样两级加载

主控不需要一开始读入所有 harness 的完整说明：

```text
L0 compact catalog
  name · summary · strengths · best_for · avoid_for · traits
                         ↓ 主控按子任务选择
L1 selected profile
  完整角色、执行契约、限制与 prompt shaping
                         ↓
L2 Herdr runtime
  动态创建或复用被选中的 harness tab
```

查看主控预加载的紧凑 catalog：

```bash
just catalog
just catalog-json
```

按需查看一个完整 profile：

```bash
just profile codex
```

catalog 的真源是 `profiles/harnesses/*.toml`，完整上下文在同目录 Markdown。planner 只收到当前 workflow 启用 harness 的紧凑 catalog；任务真正 dispatch 前，coordinator 才读取所选 harness 的完整 Markdown profile 并注入 prompt。

## 为什么不是让 Herdr 直接当主控

Herdr 提供真实 PTY、detach/reattach、agent 状态、pane 和 workspace 控制，但它不是推理 agent。这个仓库的分工是：

```text
Workflow TOML / Planner task JSON
              ↓
Planner + compact harness catalog
              ↓
Deterministic coordinator + selected full profile
  queue · lease · retry · dedupe · receipt
              ↓
         Herdr CLI runtime
              ↓
Droid · Grok Build · Codex · pi · Claude Code · Hermes
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

## 实时 Dashboard

```bash
just dashboard

# 自定义本地端口与刷新周期
just dashboard --port 9000 --poll-seconds 1
```

浏览器页面实时展示 durable queue 看板、blocked/failed/runtime drift、Herdr
workspace/tab/pane/agent 拓扑、原生 worktree 和 receipt 时间线。Dashboard 是只读投影，
默认只监听 `127.0.0.1:8765`，不读取 prompt、环境变量或 terminal output。详细行为见
[`docs/dashboard.md`](docs/dashboard.md)。

## 六 harness 真实只读 smoke

下面的命令会依次启动或复用六种 harness，要求它们只读检查两个本地配置文件，并验证六个 agent 都经历真实 turn 后回到 settled state：

```bash
just smoke

# 只验证指定 harness，可重复 --harness
just smoke --harness pi --harness claude
just smoke --harness grok
```

smoke 不把终端文本当完整 transcript，因为 full-screen agent 的历史可能不进入 Herdr scrollback。验证依据是 agent 成功启动、prompt 被接受并经过 lifecycle change 后返回 `idle` 或 `done`。临时 tab 会在成功或失败后关闭；已存在并被安全复用的 agent 不会被关闭。

若任务长时间停在 `running`、首次启动 timeout、或 agent 看似启动却没有真实执行，按
[`docs/runtime-troubleshooting.md`](docs/runtime-troubleshooting.md) 区分 provisioning、
prompt acceptance、working 与 settled，不要只根据 pane 存在或最终标题判断。

## 添加一个任务

```bash
# 显式指定 worker
just enqueue codex review docs/prompts/review.md review-docs-v1
just enqueue grok build workflows/prompts/grok-build-check.md build-v1

# 可显式覆盖执行拓扑
just enqueue codex build workflows/prompts/build.md build-v2 --placement worktree
just enqueue pi inspect workflows/prompts/pi-config-check.md inspect-v2 --placement pane

# 不指定 worker，由主控读取 compact catalog 后选择
just enqueue-auto review workflows/prompts/codex-architecture.md review-auto-v1
```

参数依次为 `harness`、`title`、`prompt_file`、`dedupe_key`。相同 workflow 下重复的 `dedupe_key` 不会重复入队。

### Tab、pane 与原生 worktree

普通 queue 默认使用混合 topology policy：

- 明确只读任务进入本轮共享 tab 的独立 pane；
- 明确会修改仓库的任务进入 Herdr 原生 worktree workspace；
- 需要完整终端但不需要 checkout 隔离时使用独立 tab；
- 模糊任务由 controller 输出受 schema 约束的 topology JSON。

内部 agent 名为了唯一性仍保留短 hash，但新建 tab/worktree 的可见标题使用截断后的任务
标题，不再显示 hash 后缀。Pane 模式按 `run_once` 批次分组；worktree 完成后保留
workspace、checkout 和 branch，不自动 merge 或删除。配置见
[`docs/workflow-schema.md`](docs/workflow-schema.md)。

## 分别选择主控与 worker

Workflow 可以固定主控和 worker 候选池，也可以使用自动选择：

```toml
[planner]
harness = "grok" # 或 "auto"，也可以省略
worker_harnesses = ["codex", "pi", "grok"]
```

命令行可临时覆盖，而不修改 TOML：

```bash
# Grok 做主控，只允许 Codex 与 pi 被派发
just run-once --controller-harness grok \
  --worker-harness codex \
  --worker-harness pi

# enqueue 未指定 --harness 时，Grok 只在给定候选池中选择 worker
PYTHONPATH=src python3 -m herdr_orchestrator enqueue \
  --workflow workflows/multi-harness.toml \
  --controller-harness grok \
  --worker-harness codex \
  --worker-harness pi \
  --title review \
  --prompt-file workflows/prompts/codex-architecture.md \
  --dedupe-key review-auto-v2
```

未指定主控时，coordinator 按 `droid → grok → codex → claude → hermes → pi` 的固定优先级，从候选 worker 中选择本机已安装的 CLI；这一步只检查 executable，不代表认证健康。未指定 worker 时，选中的主控只收到候选池 compact catalog，并写出严格的 `{"harness":"..."}` JSON，coordinator 校验后才入队。显式指定 worker 时不会额外启动 router turn。

## Opt-in 标准化交付

仓库提供一个默认不触发的 Skill：

- 显式调用：`/standardized-delivery`、`/matt-workflow`、`/wayfinder-delivery`
- 精确关键词：`标准化交付`、`完整工程流程`、`Matt workflow`、`Pocock workflow`、
  `Wayfinder 全流程`、`自主交付`

普通的“实现”“修复”“计划”“review”或“orchestrate”请求不会进入该流程。明确触发后，
coordinator 执行适配后的完整链：

```text
Wayfinder auto-route
  ├─ clear/small ─────────────────────────────┐
  └─ large + foggy → decision map → clear ───┤
                                              ▼
to-spec → tracer-bullet ticket DAG
               ↓ frontier, max 3
per-ticket isolated worktree + fresh worker + commit + receipt
               ↓ close criteria and advance blockers
single final review: Standards ∥ Spec
               ↓ controller adjudication
bounded repair + re-review, at most 2 rounds
```

主控在此 opt-in 模式下是 principal proxy：它代替用户处理规格内的本地、可逆选择和
harness approval/question。secret、credential、token、password、production system 与
production data 始终升级给用户。决策写入 runtime ledger，但 worker answer 和 secret
不会写入 ledger。

先把目标写入 ignored runtime 文件，再执行：

```bash
mkdir -p .orchestrator/requests
$EDITOR .orchestrator/requests/my-goal.md
just deliver .orchestrator/requests/my-goal.md

# 临时固定主控和 worker pool
just deliver .orchestrator/requests/my-goal.md \
  --controller-harness grok \
  --worker-harness codex \
  --worker-harness droid
```

默认 tracker 是 `.scratch/standardized-delivery/<slug>/` 下的 local Markdown，每张
ticket 一个文件。也可在 TOML 或 CLI 中明确选择 GitHub Issues。GitHub backend 的
显式触发只授权该次交付的 issue 创建、更新与关闭，不授权 push、PR、merge、release
或 deploy。

成功结果停在独立 integration branch，不会自动落到用户当前 branch。详细配置、阶段、
artifact、恢复和退出码见
[`docs/standardized-delivery.md`](docs/standardized-delivery.md)。

## 工作流

首个示例是 [`workflows/multi-harness.toml`](workflows/multi-harness.toml)。它声明：

- coordinator 的轮询、并发、lease 和重试策略；
- 六个 harness worker，包括 Grok Build；
- `profiles_dir` 指向 harness catalog；
- 可选 planner agent；
- 可幂等 seed 的示例任务。

配置说明见 [`docs/workflow-schema.md`](docs/workflow-schema.md)，运行与恢复语义见 [`docs/architecture.md`](docs/architecture.md)。

## 明确不做

- 不把 `done` 当成质量证明；
- 不在普通 queue 模式自动回答 approval 或 question UI；
- 不自动 push、merge、发布或删除；
- 不把 pane terminal output 当完整 transcript；
- 不让 planner 生成并执行任意 shell command；
- 不在 v1 内做跨机器分布式调度。
