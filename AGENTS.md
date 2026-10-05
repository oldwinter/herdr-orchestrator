# AGENTS.md

本地优先的多 harness 工作流控制面：声明式 TOML 描述工作流，确定性 coordinator 管理 durable queue、lease、重试与收据，Herdr 只承载交互式 agent（terminal runtime，不是推理主控）。planner 只提出符合 schema 的任务，入队、调度、恢复由 coordinator 决定。

## Start

1. `just doctor`，`just test`
2. 读 `docs/architecture.md`（恢复、lease、planner、delivery 语义），再看 `workflows/*.toml`；改 TOML 字段前读 `docs/workflow-schema.md`。
3. 选 worker 先跑 `just catalog`，只加载被选中 harness 的 `profiles/harnesses/<name>.md`。

## 运行模式

各模式状态语义独立，不要混用：

| 模式 | 入口 | 何时用 |
| --- | --- | --- |
| Durable queue | `just seed` / `enqueue` / `enqueue-auto` / `run` / `run-once` / `run-until-idle` / `retry` / `gc` / `status` | 普通派发、重试、收据、无人值守 |
| Manual manager | `just manager [harness]` | 当前 Herdr session 内的专用交互管理会话，策略见 `manager/AGENTS.md` |
| Read-only dashboard | `just dashboard` | 查看 queue、attention、拓扑与 receipt 时间线，见 `docs/dashboard.md` |
| Standardized delivery | 仅 `just deliver` 或显式 Skill | 用户明确触发时才用；入口 `.agents/skills/standardized-delivery/SKILL.md`，细节 `docs/standardized-delivery.md` |

## 真源

- `src/herdr_orchestrator/`：调度、状态与 Herdr adapter。
- `profiles/harnesses/*.toml`：紧凑 harness catalog（planner 只看到当前 workflow 启用的部分）；同名 `.md` 是 dispatch 前才注入的完整上下文。
- `.agents/skills/` 是 Skill 真源，`.agent/skills`、`.claude/skills` 只是兼容 symlink。
- `tests/` 是行为契约；`.orchestrator/` 是本机 runtime state，禁止提交。
- 分发：`bin/herdr-orchestrator.mjs` + `package.json`（npm）、`packages/herdr-manager/`、`skills/herdr-orchestrator/`（`npx skills`）；发布 gate 是 `scripts/npm-release-plan.mjs` 与 `.github/workflows/ci.yml`。

## 安全边界

- secret 只从环境变量、keychain 或 harness 自身登录态读取。
- planner 输出只接受受 schema 校验的 task JSON；coordinator 拒绝 `command`、`argv` 等字段，也不执行模型提交的 shell。
- 这个输出校验不是进程或工具沙箱：planner 仍按所选 harness 的最高自动化参数运行，六种 harness 没有可移植的 no-tools 模式，被攻陷的 harness 仍可能使用自身工具。planner 只能从当前 workflow 启用的 catalog 中选 worker。
- worktree 只是 checkout 隔离，不是安全沙箱。
- 默认任务不得 push、merge、发布、发送、删除、修改权限或触碰生产环境。标准交付只在显式触发后启用 principal proxy，secret 与 production 仍升级给用户；`github` tracker 只授权该次交付的 issue 增改关，不授权 push、PR、merge、release、deploy。
- `blocked`、`unknown`、timeout 都不是成功；`idle` / `done` 只表示 agent settled，声明 task receipt 必须 `task_verified=true`。
- 普通 queue 的 `blocked` 是 terminal，只能人工 `resume --response-file` 恢复；只有标准交付有自动的有界 controller response loop。
- 所有 Herdr wait 必须有 timeout；不关闭非本运行创建的 pane 或 agent。
- runtime state、完整终端输出和原始 prompt 不进 Git。
- npm Trusted Publishing 不支持 self-hosted runner：只有可信 `main` 的版本 gate 用专属 runner，PR 测试和 `publish` 保持 GitHub-hosted，不引入长期 npm token。
- `herdr-manager` 只能用固定 argv 转发到 `herdr-orchestrator manager`，不用 shell，默认 harness 只来自 `grok → codex → claude` allowlist。

## 修改约定

- Python 3.12+ 标准库；新增依赖先说明必要性。
- 配置 schema、SQLite migration 和 receipt 保持向后兼容。
- CLI 输出面向 automation：失败给稳定错误码或明确原因。
- 改后跑最小相关 unittest，收口前 `just check`。
