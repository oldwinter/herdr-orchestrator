# AGENTS.md

## 仓库定位

本仓库是本地优先的多 harness 工作流控制面。它用声明式 TOML 描述工作流，由确定性 coordinator 管理 durable queue、lease、重试与收据，并通过 Herdr 承载交互式 agent。

Herdr 是 terminal runtime，不是推理主控。planner agent 只能提出符合 schema 的任务，是否入队、何时调度、如何恢复由 coordinator 决定。

## Start

1. `just doctor`
2. `just test`
3. 阅读 `docs/architecture.md`
4. 查看 `workflows/multi-harness.toml`

## Canonical Surface

- `src/herdr_orchestrator/`：调度、状态与 Herdr adapter 真源
- `workflows/*.toml`：工作流配置真源
- `workflows/prompts/`：任务 prompt 文件
- `.orchestrator/`：本机 runtime state，禁止提交
- `tests/`：行为契约
- `justfile`：稳定命令入口

## 安全边界

- secret 只从环境变量、keychain 或 harness 自身登录态读取。
- planner 不得直接提交 shell 命令，只能输出受校验的 task JSON。
- 默认任务不得 push、merge、发布、发送、删除、修改权限或触碰生产环境。
- worktree 只是 checkout 隔离，不是安全沙箱。
- `blocked`、`unknown` 和 timeout 都不是成功。
- 所有 Herdr wait 必须有 timeout。
- 不关闭非本运行创建的 pane 或 agent。
- runtime state、完整终端输出和原始 prompt 不进入 Git。

## 修改约定

- 使用 Python 3.12+ 标准库，新增依赖前先说明必要性。
- 保持配置 schema、SQLite migration 和 receipt 向后兼容。
- CLI 输出应适合 automation，失败必须给稳定错误码或明确原因。
- 修改后运行最小相关 unittest，收口前运行 `just check`。
