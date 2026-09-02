# herdr-orchestrator

基于 Herdr 的本地优先多 harness 工作流控制面。

它让一个确定性 coordinator 持续派发任务给 Droid、Grok Build、Codex、pi、Claude Code、Hermes 等交互式 agent，同时保留 durable queue、lease、重试、去重和收据。可选 planner agent 只向 coordinator 提交结构化任务，不拥有 queue 调度权限；planner 进程仍可使用所选 harness 的工具，也不是安全沙箱。

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

catalog 的真源是 `profiles/harnesses/*.toml`，完整上下文在同目录 Markdown。planner 只收到当前 workflow 启用 harness 的紧凑 catalog；任务真正 dispatch 前，coordinator 才读取所选 harness 的完整 Markdown profile 并注入 prompt。coordinator 只接受经过 schema 校验的 task JSON，并拒绝 `command`、`argv` 等字段；这不限制 planner harness 自己使用工具，也不把进程变成沙箱。

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
- Node.js 20+（仅一键安装与 `npx` 包装需要）
- `just`（仅源码 checkout 开发需要）

## 一键安装到其他仓库

Skill 和 runtime 是两个互补入口。Skill 让 coding agent 知道何时、如何使用；npm CLI
负责确定性安装 Python runtime、workflow 和 harness profiles。

```bash
# 安装 agent Skill，可用 -g 改为用户级安装
npx skills add oldwinter/herdr-orchestrator \
  --skill herdr-orchestrator --agent '*' -y

# 在目标 Git 仓库安装 runtime 与默认 workflow
cd /path/to/target-repository
npx --yes herdr-orchestrator install --project .
npx --yes herdr-orchestrator doctor --project .
```

`doctor` 不是纯静态检查。对于环境与 CLI 均可用的 harness，它会启动或复用 agent，提交一个
带 output receipt 的真实只读 readiness turn，并在 probe 后关闭本次创建的临时 agent。

安装器默认检测本机可执行的 harness。也可显式固定：

```bash
npx --yes herdr-orchestrator install --project . \
  --harness droid --harness codex
```

目标仓已存在 `.agents/skills/` 路由时，runtime installer 默认不注入项目 Skill；需要时显式
加 `--install-skill`，或用上面的 `npx skills` 独立安装。installer 托管的生成目录通过
仓库本地 `.git/info/exclude` 隐藏，不修改项目的已跟踪 `.gitignore`，也不隐藏独立安装的
Skill。项目内 symlinked `.git` / exclude 会在写入前被拒绝；Git 原生 linked worktree 的
外部 common Git dir 仍受支持。

它只管理 `.herdr-orchestrator/`、`.agents/skills/herdr-orchestrator/` 和
`.orchestrator/.gitignore` 中 manifest 记录的文件。重复安装或升级不会覆盖用户修改；
卸载也只删除 hash 未变化的托管文件。若 Skill 已由 `npx skills` 安装，npm installer
会复用但不会接管它。

```bash
npx --yes herdr-orchestrator upgrade --project .
npx --yes herdr-orchestrator uninstall --project .
```

完整安装契约见 [`docs/installation.md`](docs/installation.md)。

## 手动 Herdr 管理会话

需要临场观察、协调当前 Herdr session，而不需要 durable queue 时，在源码 checkout 中运行：

```bash
just manager         # 自动选择 Grok、Codex、Claude
just manager claude  # 或显式选择 harness
```

新环境中最短的一次性入口是：

```bash
npx --yes herdr-manager
npx --yes herdr-manager claude
```

未显式指定 harness 时，launcher 按 `grok → codex → claude` 检查本机 CLI，并启动第一个
可用项；三者都不可用时会给出明确错误。该独立入口只要求 Node.js 20+、Herdr 和所选
harness，不需要 clone 本仓库、安装项目 runtime 或提供 `--project`。

高频使用可从源码 checkout 一次性安装全局命令，之后从任意目录启动：

```bash
just install-manager
herdr-manager
herdr-manager claude
```

`install-manager` 先安装全局命令，再运行 `herdr-orchestrator manager-light install`。
后者要求 Herdr 0.8.2 或更高版本，安装包内插件，并以原子、候选配置已校验的方式接管一个
带 marker 的 `[ui.sidebar.agents]` block。它拒绝覆盖已有的 Agent rows；block 之外的配置字节
保持不变。蓝色实心灯表示 manager，其他 Agent 仍由 Herdr 真实生命周期投影为 blocked、
working、idle 或 unknown，manager launcher 不会改写 Agent lifecycle。
自定义 rows 只作用于桌面端展开的 Agent sidebar；折叠 sidebar 和移动端继续使用 Herdr
内建 indicator。

`install-manager` 由 `just` 的非交互 shell 调用 npm，因此不会命中把
`npm install --global .` 重写为 `mise use -g npm:.` 的交互式 wrapper。发布版 `0.1.3`
及以上也可直接用 `npm install --global herdr-orchestrator` 安装；单独运行 npm 安装不会修改
Herdr 配置。检查或移除投影时运行：

```bash
herdr-orchestrator manager-light status
herdr-orchestrator manager-light uninstall
```

如果 `herdr-manager` 包暂时不可用，也可用显式 npm package/bin 形式：

```bash
npm exec --yes --package herdr-orchestrator -- herdr-manager claude
```

这些入口都要求 `HERDR_ENV=1`，并把所选 harness 无附加参数地启动在包内固定的 manager 目录。该目录
中的短 policy 要求会话只观察和操作当前 Herdr session，把 terminal output 当作不可信数据，
并在每次动作后重新读取状态。它不维护插件协议、模型表、队列或后台进程。

旧的 `manager --project <path> --harness <name>` 形式继续兼容，用于显式采用目标项目中由
installer 托管的 manager workspace；普通手动管理不需要安装项目 runtime 或传 `--project`。

需要无人值守派发、重试、去重、lease 和机器收据时，仍使用下面的 durable queue。manager
看到 agent 进入 idle/done 也不能据此宣称任务成功，必须另行核验产物或 receipt。

## 源码 checkout 快速开始

```bash
uv sync --locked
just doctor
just test

# 把示例任务幂等写入 durable queue
just seed

# 处理一个 replica-limited wave 后退出
just run-once

# 多波处理，直到当前 worker pool 无 pending/running
just run-until-idle

# 持续运行，detach Herdr 后 coordinator 与 agents 继续工作
just run

# 查看任务状态
just status
```

开发环境、devcontainer、质量门禁与 Git hooks 见
[`docs/development.md`](docs/development.md)。机器生成的完整 CLI 参数参考见
[`docs/generated/cli.md`](docs/generated/cli.md)。

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

Coordinator 为每次 dispatch 生成 correlation ID，并在本机记录经过脱敏的结构化事件、
指标与告警。Sentry、PostHog 和 HTTPS webhook exporter 全部默认关闭，配置、隐私边界和
告警 runbook 见 [`docs/observability.md`](docs/observability.md)。

## 六 harness 真实只读 smoke

下面的命令会依次启动或复用六种 harness，要求它们只读检查目标仓实际存在的 README 与
workflow，并验证六个 agent 都经历真实 turn、回到 settled state 且输出机器收据：

```bash
just smoke

# 只验证指定 harness，可重复 --harness
just smoke --harness pi --harness claude
just smoke --harness grok
```

smoke 不把终端文本当完整 transcript，因为 full-screen agent 的历史可能不进入 Herdr
scrollback。验证依据是 agent 成功启动、prompt 被接受、经过 lifecycle change 后返回
`idle` 或 `done`，并且 detection output 含指定行前缀。临时 tab 会在成功或失败后关闭；
已存在并被安全复用的 agent 不会被关闭。

## 结构化 readiness matrix

`readiness-matrix` 从当前 Herdr-managed pane 对启用的 harness 运行真实只读 probe。它记录当前
Git commit、package version、workflow、canonical workspace digest、每个 harness 的稳定状态、
phase timings、观测时间和 attempt count：

```bash
just readiness-matrix
just readiness-matrix --harness codex --harness claude
```

只有最终状态为 `ready` 的当前证据标记为 `VERIFIED`。失败、缺失、过期或环境不可用的条目均为
`NOT VERIFIED`。`herdr_timeout`、`timeout`、`prompt_acceptance_timeout`、
`agent_provider_failed`、`agent_turn_not_observed`、`herdr_invalid_response`、
`task_receipt_missing` 和 `readiness_probe_failed` 最多重试一次；认证、无效 model、缺少 executable
或 profile 不自动重试。输出不包含 prompt、credential、terminal output、完整 response 或任意
provider 错误文本。

Matrix 在所有 probe 前后都读取 HEAD、`git status --porcelain=v1 --untracked-files=all`，再读取
HEAD 确认同一次 sample 没有跨 commit。Initial tracked modification、staged change、untracked file 或
无法检查 source state 时，每个 selected harness 都返回 zero-attempt `readiness_source_dirty`。若
probe 期间 working tree 或 HEAD 改变，所有 rows 改为 `readiness_source_changed`。Dirty 或 drifted
source 都不能产生 `VERIFIED` 或 exit 0。

真实 matrix 只在 operator-controlled Herdr pane 运行，不进入 pull-request CI。检测到 `CI` 或
`GITHUB_ACTIONS` 时，每个 selected harness 都返回 zero-attempt `readiness_ci_forbidden`，不会调用
probe。Matrix 证明当前机器与 harness 的 dispatch compatibility，不证明 queue 单测、代码质量、
部署或产品验收。Issue #39 单独负责让自动 routing 消费 readiness evidence；这个命令不改变
worker eligibility。

若任务长时间停在 `running`、首次启动 timeout、或 agent 看似启动却没有真实执行，按
[`docs/runtime-troubleshooting.md`](docs/runtime-troubleshooting.md) 区分 provisioning、
prompt acceptance、working 与 settled，不要只根据 pane 存在或最终标题判断。

## 最高自动化启动策略

Coordinator 新建 harness agent 时会通过 Herdr 的原生参数分隔符传入固定的最高自动化参数：

| Harness | 原生启动参数 |
| --- | --- |
| Droid | `--auto high` |
| Grok Build | `--always-approve --permission-mode bypassPermissions` |
| Codex | `--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust` |
| pi | `--approve` |
| Claude Code | `--dangerously-skip-permissions` |
| Hermes | `--yolo --accept-hooks` |

这些参数减少本地工具执行、项目 trust、hook 与 sandbox 的人工确认，也意味着 worker 可在其
execution root 内直接执行高权限本地动作。参数由 control plane 固定映射，planner 和任务
prompt 都不能注入或覆盖。Claude 没有跳过首次 workspace trust 的原生参数；新建 Claude
agent 只有在 detection output 同时匹配内置 workspace trust 提示的三个稳定标记和预期
execution root 时，才会自动发送一次 Enter，其他 startup block 不会得到输入。它们不扩大
任务授权范围：push、
merge、发布、发送、删除 worktree、权限变更和生产操作仍必须由用户单独明确授权。真正的
需求问题仍可能进入 `blocked`，普通 queue 继续要求显式 `resume`。

## 添加一个任务

```bash
# 显式指定 worker
just enqueue codex review workflows/prompts/codex-architecture.md review-docs-v1
just enqueue grok build workflows/prompts/grok-build-check.md build-v1

# 可显式覆盖执行拓扑，即使静态规则会为只读 review 选择 pane
just enqueue codex review-isolated workflows/prompts/codex-architecture.md review-isolated-v1 --placement worktree
just enqueue pi inspect workflows/prompts/pi-config-check.md inspect-v2 --placement pane

# 需要内容级机器验收时声明 output 或 file receipt（二选一）
just enqueue pi inspect workflows/prompts/pi-config-check.md inspect-v3 \
  --placement pane --receipt-prefix "TASK-OK inspect"

# 新任务可要求绑定当前 job / attempt / fencing token 的 structured-v2 envelope
just enqueue codex inspect workflows/prompts/codex-architecture.md inspect-v4 \
  --placement pane --completion-policy structured-v2

# 不指定 worker，由主控读取 compact catalog 后选择
just enqueue-auto review workflows/prompts/codex-architecture.md review-auto-v1
```

参数依次为 `harness`、`title`、`prompt_file`、`dedupe_key`。相同 workflow 下重复的 `dedupe_key` 不会重复入队。

Completion policy 分为 `legacy-unverified`、`receipt-v1` 和 `structured-v2`。未声明 evidence 的
兼容任务使用 `legacy-unverified`；output-prefix 与 file receipt 使用 `receipt-v1`；显式
`--completion-policy structured-v2` 的任务由 coordinator 在 claim 后追加 job ID、attempt 和
fencing token，原始 prompt、planner 或 router 不能覆盖这些字段。

`status` 同时展示 `agent_settled`、兼容字段 `task_verified`、`completion_policy`、
`verification_class`、`completion_status`、有界 evidence summary 和稳定 completion error。
历史 `succeeded` 状态不会因 migration 改写。Structured envelope 只证明当前 attempt 产生了声明的
机器证据；它不证明 code review、产品验收、release 或 deployment。Idempotent task 即使没有改变
业务文件，也可用当前 attempt identity 报告已存在的正确结果。

## 排空、重试与回收

`run-once` 的 `batch` 是本波结果，`queue` 是结束时全局计数；replica=1 时同 harness 的
多条任务需要多波。普通运行优先使用：

```bash
just run-until-idle --drain-timeout-seconds 86400

# failed job 原 id / dedupe_key 保持不变，只追加一次 attempt budget
just retry 42 --extra-attempts 1

# blocked job 仅在人工审查后显式回答；复用原 agent/pane/attempt，不重发任务 prompt
just resume 43 approval.txt

# 默认只预览；--apply 只关闭本 workflow 已成功且 settled 的 agent pane
just gc
just gc --apply

# failed 资源使用独立显式 scope；blocked 永不进入常规 GC
just gc-failed
```

GC 永不关闭或删除 worktree workspace、checkout 与 branch，也不碰 foreign/active/blocked agent。
回收还必须同时有 `member_reused=false` 的创建收据，且当前 pane ID 与收据一致；tab placement
也只关闭该 pane，不关闭承载它的整个 tab。

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

未指定主控时，coordinator 按 `droid → grok → codex → claude → hermes → pi` 的固定优先级，只从具有 fresh `ready` health evidence 的候选 worker 中选择。unknown/过期 evidence 会接受一次 bounded refresh；认证、invalid model、provider 或 integration failure 会被排除，并报告每个 harness 的稳定 reason。未指定 worker 时，选中的主控只收到 eligible 候选池 compact catalog，并写出严格的 `{"harness":"..."}` JSON，coordinator 校验后才入队。显式指定 harness 不会静默 fallback。

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

仓库跟踪多个声明式 workflow。默认多 harness 示例是
[`workflows/multi-harness.toml`](workflows/multi-harness.toml)，研究示例是
[`workflows/grok-research.toml`](workflows/grok-research.toml)。多 harness 示例声明：

- coordinator 的轮询、并发、lease 和重试策略；
- 六个 harness worker，包括 Grok Build；
- `profiles_dir` 指向 harness catalog；
- 可选 planner agent；
- 可幂等 seed 的示例任务。

配置说明见 [`docs/workflow-schema.md`](docs/workflow-schema.md)，运行与恢复语义见 [`docs/architecture.md`](docs/architecture.md)。

## 明确不做

- 不把 `done` 当成质量证明；
- 不在普通 queue 模式自动回答 job approval 或需求 question UI；启动期只自动确认精确匹配的 Claude workspace trust；
- 不自动 push、merge、发布或删除；
- 不把 pane terminal output 当完整 transcript；
- 不把 planner 输出中的 `command` 或 `argv` 字段交给 coordinator 执行；planner harness 自身的工具使用不受这个数据校验保证，worktree 也不是安全沙箱；
- 不在 v1 内做跨机器分布式调度。
