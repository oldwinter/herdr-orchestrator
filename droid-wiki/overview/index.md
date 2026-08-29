# Herdr Orchestrator

Herdr Orchestrator 是一个本地优先的多 harness 工作流控制面。它把任务写入 SQLite durable queue，由确定性的 coordinator 负责 lease、重试、并发和收据，再通过 Herdr 的真实 PTY、tab、pane 与 worktree 启动 Droid、Grok Build、Codex、pi、Claude Code 或 Hermes。

## 适合谁

- 需要把多种 coding agent 放进同一套可恢复队列的开发者。
- 需要在 Herdr 中长期运行任务，同时保留 attempt、lease 与机器验收记录的维护者。
- 只想临时观察当前 Herdr session，而不需要 durable queue 的操作员。这个场景使用手动 Manager。
- 需要只读查看 queue、agent lifecycle、worktree 和 receipt 的本地操作者。

## 四个运行面

| 运行面 | 入口 | 核心语义 |
| --- | --- | --- |
| Durable queue | `run`、`run-once`、`run-until-idle` | SQLite queue、lease、replica、retry、resume、receipt |
| 手动 Manager | `herdr-manager` 或 `just manager` | 当前 Herdr session 内的单个交互式管理 agent，不持久化调度状态 |
| 只读 Dashboard | `just dashboard` | 关联 durable state 与 Herdr runtime，但不修改两者 |
| 标准化交付 | `deliver` 或明确 Skill 触发 | Wayfinder、规格、ticket DAG、独立 worktree、双轴 review |

这些入口共享 Python 控制面，但不能混用成功语义。`idle` 或 `done` 只表示 agent settled；声明 task receipt 的任务还必须得到 `task_verified=true`。边界定义见 `AGENTS.md`，完整结构见[系统架构](architecture.md)。

## 仓库地图

| 路径 | 内容 |
| --- | --- |
| `src/herdr_orchestrator/` | Python 3.12 控制面、CLI、存储、Herdr adapter、Dashboard |
| `workflows/` | 声明式 TOML workflow 与任务 prompt |
| `profiles/harnesses/` | 两级 harness catalog 的 compact metadata 与完整执行 profile |
| `bin/herdr-orchestrator.mjs` | npm 安装器、runtime 包装器、Manager 与 Manager Light 命令 |
| `manager/` | 手动 Manager 的固定工作目录与 policy |
| `plugins/manager-light/` | Herdr 插件和 sidebar token 投影 |
| `packages/herdr-manager/` | `npx herdr-manager` 的薄 npm 入口 |
| `tests/` | 按行为契约组织的 Python 与 Node 驱动测试 |
| `docs/` | 架构、安装、运行诊断、Dashboard、可观测性与 schema 文档 |

## 从哪里开始

1. 从源码开发，按[快速开始](getting-started.md)执行 `uv sync --locked`、`just doctor` 和 `just test`。
2. 在其他仓库使用，先看[安装与分发](../systems/installation-and-distribution.md)。
3. 理解普通队列，阅读[Coordinator 与 durable queue](../systems/coordinator-and-queue.md)。
4. 诊断运行问题，阅读[Herdr runtime](../systems/herdr-runtime.md)和[调试手册](../how-to-contribute/debugging.md)。
5. 查询参数，查看[CLI 参考](../reference/cli-reference.md)。

## 关键源文件

| 文件 | 用途 |
| --- | --- |
| `src/herdr_orchestrator/cli.py` | Python CLI 命令、doctor 与 smoke 入口 |
| `src/herdr_orchestrator/runner.py` | Coordinator 调度、排空、retry/resume/GC 协调 |
| `src/herdr_orchestrator/store.py` | SQLite schema、migration、claim、receipt |
| `src/herdr_orchestrator/herdr.py` | Agent lifecycle、prompt acceptance、机器收据验证 |
| `src/herdr_orchestrator/herdr_layout.py` | tab、pane、原生 worktree 的 provision 与清理 |
| `bin/herdr-orchestrator.mjs` | npm 安装、升级、卸载与 Manager 包装 |
