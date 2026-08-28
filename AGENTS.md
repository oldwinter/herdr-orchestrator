# AGENTS.md

## 仓库定位

本仓库是本地优先的多 harness 工作流控制面。它用声明式 TOML 描述工作流，由确定性 coordinator 管理 durable queue、lease、重试与收据，并通过 Herdr 承载交互式 agent。

Herdr 是 terminal runtime，不是推理主控。planner agent 只能提出符合 schema 的任务，是否入队、何时调度、如何恢复由 coordinator 决定。

## Start

1. `just doctor`
2. `just test`
3. 阅读 `docs/architecture.md`
4. 查看 `workflows/multi-harness.toml`
5. 需要选 worker 时先跑 `just catalog`，不要一次读完所有 harness `.md`

## 工作流

当前只有一个声明式工作流：`workflows/multi-harness.toml`。它声明 coordinator 策略、六个 worker、紧凑 catalog 目录、可选 planner，以及可幂等 seed 的示例任务。改 TOML 字段前读 `docs/workflow-schema.md`。

仓库运行面各自独立，不要混用其状态语义：

| 模式 | 入口 | 何时用 |
| --- | --- | --- |
| Durable queue | `just seed` / `enqueue` / `enqueue-auto` / `run` / `run-once` / `run-until-idle` / `retry` / `gc` / `status` | 普通派发、重试、收据、无人值守 queue |
| Manual manager | `just manager [harness]` | 在当前 Herdr session 内启动一个专用交互式管理会话 |
| Read-only dashboard | `just dashboard` | 实时查看 queue、attention、Herdr topology 与 receipt timeline |
| Standardized delivery | 仅 `just deliver` 或显式 Skill | 用户明确触发标准交付；普通实现/修复/review/orchestrate 不走这条路 |

标准交付入口是 `.agents/skills/standardized-delivery/SKILL.md`；`matt-workflow` 与 `wayfinder-delivery` 只是别名。阶段、artifact、退出码和恢复读 `docs/standardized-delivery.md`。

示例 workflow 里 planner 默认关闭。主控与 worker 候选可在 TOML 或 CLI `--controller-harness` / `--worker-harness` 指定；未指定主控时按 `droid → grok → codex → claude → hermes → pi` 选本机已安装 CLI。

## 能力

- **六 harness**：`droid`、`grok`、`codex`、`pi`、`claude`、`hermes`。能力描述真源是 `profiles/harnesses/*.toml`；完整执行上下文在同名 `.md`，只在该 harness 被选中后加载。
- **两级 catalog**：planner / router 只看到当前 workflow 启用的紧凑 catalog；coordinator 在 dispatch 前才注入所选完整 profile。
- **Durable queue**：claim、lease、重试、`dedupe_key`、收据。任务状态见 `docs/architecture.md`。
- **Manual manager**：固定工作目录中的单个 harness 会话，只管理当前 Herdr session；不模拟 queue、retry、lease 或 receipt。
- **自动选 worker**：`enqueue` 省略 harness 时，主控只输出 `{"harness":"..."}`，coordinator 校验后入队。
- **只读 smoke**：`just smoke` 对启用 harness 做真实 turn 连通；可用重复 `--harness` 收窄。
- **实时 Dashboard**：本地只读 Web 投影，异步展示 durable queue、runtime drift、
  Herdr workspace/tab/pane/worktree 拓扑与 receipt 时间线。
- **Opt-in 标准交付**：Wayfinder（仅在有 decision fog 时）→ spec + ticket DAG → 独立 worktree 实现 → Standards ∥ Spec review → 至多 2 轮 repair。成功停在隔离 integration branch，不自动 push / merge / deploy。
- **Tracker**：默认 local Markdown；`github` 只授权该次交付的 issue 创建、更新与关闭。

稳定命令入口是 `justfile`。常用：`manager`、`install-manager`、`doctor`、`test`、`check`、`catalog`、`profile`、`seed`、`enqueue`、`enqueue-auto`、`status`、`dashboard`、`run`、`run-once`、`run-until-idle`、`retry`、`gc`、`deliver`、`smoke`。

## Canonical Surface

- `src/herdr_orchestrator/`：调度、状态与 Herdr adapter 真源
- `workflows/*.toml`：工作流配置真源
- `workflows/prompts/`：任务 prompt 文件
- `profiles/harnesses/*.toml`：主控预加载的紧凑 harness catalog 真源
- `profiles/harnesses/*.md`：选中 harness 后才按需加载的完整执行上下文
- `manager/`：手动 Herdr 管理会话的 canonical policy 与 Claude adapter
- `.agents/skills/standardized-delivery/`：opt-in 标准交付 Skill 与分阶段 reference
- `.agent/skills`、`.claude/skills`：指向 `.agents/skills` 的兼容 symlink，不是独立真源
- `docs/architecture.md`：恢复、lease、planner、delivery 运行语义
- `docs/dashboard.md`：只读实时 Dashboard、SSE 和数据安全语义
- `docs/workflow-schema.md`：改 workflow / delivery TOML 字段时读
- `docs/standardized-delivery.md`：仅在用户明确触发标准交付时读
- `.orchestrator/`：本机 runtime state，禁止提交
- `tests/`：行为契约
- `justfile`：稳定命令入口
- `skills/herdr-orchestrator/`：`npx skills` 分发的通用 orchestrator Skill
- `bin/herdr-orchestrator.mjs`：npm 安装、诊断、升级、卸载和 runtime 包装入口
- `package.json`：`herdr-orchestrator` npm 分发清单
- `packages/herdr-manager/`：提供 `npx herdr-manager` 的薄 npm 入口
- `scripts/npm-release-plan.mjs`：main 发布前的 registry 版本 gate
- `.github/workflows/ci.yml`：专属 self-hosted 版本 gate 与 GitHub-hosted 测试/npm OIDC 发布编排

## 安全边界

- secret 只从环境变量、keychain 或 harness 自身登录态读取。
- planner 不得直接提交 shell 命令，只能输出受校验的 task JSON。
- planner 只能从当前 workflow 启用的紧凑 harness catalog 中选择 worker。
- 默认任务不得 push、merge、发布、发送、删除、修改权限或触碰生产环境。
- 标准交付模式仅在明确 Skill/CLI 触发后启用 principal proxy；规格内本地动作可自主决定，
  secret 与 production 必须升级给用户。
- `github` tracker backend 只授权创建、更新与关闭该次交付的 issues，不授权 push、PR、
  merge、release 或 deploy。
- worktree 只是 checkout 隔离，不是安全沙箱。
- `blocked`、`unknown` 和 timeout 都不是成功。
- `idle` / `done` 只表示 agent settled；声明 task receipt 时必须 `task_verified=true`。
- 所有 Herdr wait 必须有 timeout。
- 不关闭非本运行创建的 pane 或 agent。
- 普通 queue 的 `blocked` 是 terminal，不自动回答；只有人工显式 `resume --response-file`
  可恢复原 agent/pane/attempt。仅 opt-in 标准交付执行自动的有界 controller response loop。
- runtime state、完整终端输出和原始 prompt 不进入 Git。
- npm Trusted Publishing 不支持 self-hosted runner；只有可信 `main` 的版本 gate 使用专属
  runner，PR 测试和 `publish` 必须保持 GitHub-hosted，且不得引入长期 npm token。
- `herdr-manager` 只能用固定 argv 转发到 `herdr-orchestrator manager`，不得使用 shell，
  默认 harness 候选只能来自 `grok → codex → claude` 固定 allowlist。

## 修改约定

- 使用 Python 3.12+ 标准库，新增依赖前先说明必要性。
- 保持配置 schema、SQLite migration 和 receipt 向后兼容。
- CLI 输出应适合 automation，失败必须给稳定错误码或明确原因。
- 修改后运行最小相关 unittest，收口前运行 `just check`。
