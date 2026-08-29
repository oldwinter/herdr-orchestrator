# 术语表

本页解释代码、CLI 和运行文档中反复出现的项目术语。建议先理解 durable queue、harness、placement 和 receipt，再阅读系统实现。

| 术语 | 含义 | 主要定义 |
| --- | --- | --- |
| Harness | 被 Herdr 承载的交互式 agent CLI。当前支持 Droid、Grok、Codex、pi、Claude、Hermes | `src/herdr_orchestrator/model.py` |
| Herdr | 提供真实 PTY、workspace、tab、pane、agent lifecycle 和原生 worktree 的 terminal runtime | `src/herdr_orchestrator/herdr.py` |
| Coordinator | 拥有 claim、lease、wave、retry、placement 和 outcome 持久化的确定性调度器 | `src/herdr_orchestrator/runner.py` |
| Durable queue | SQLite 中可在 coordinator 重启后恢复的任务队列 | `src/herdr_orchestrator/store.py` |
| Planner | 可选的任务提出者，只能写严格 task JSON，不拥有调度权 | `src/herdr_orchestrator/planner.py` |
| Controller / router | 为单个任务选择 harness 或 topology 的受限 agent turn | `src/herdr_orchestrator/selection.py`、`src/herdr_orchestrator/topology.py` |
| Compact catalog | Planner/Router 可见的 harness 摘要，不含完整执行上下文 | `src/herdr_orchestrator/catalog.py` |
| Full profile | Harness 被选中后才读取的 Markdown 执行契约 | `profiles/harnesses/*.md` |
| Placement | 任务在 Herdr 中的执行位置，取值为 `tab`、`pane`、`worktree` | `src/herdr_orchestrator/model.py` |
| Wave | 一次 `run_once` claim 并并发处理的任务集合 | `src/herdr_orchestrator/runner.py` |
| Replica | 同一种 harness 可并发占用的 slot 数 | `workflows/multi-harness.toml` |
| Lease | Running job 在一段时间内的所有权。过期后可回收或失败 | `src/herdr_orchestrator/store.py` |
| Attempt | Job 每次成功 claim 的序号 | `src/herdr_orchestrator/store.py` |
| `dedupe_key` | Workflow 内防止重复入队的稳定键 | `src/herdr_orchestrator/model.py` |
| Agent settled | Agent 的当前 turn 稳定进入 `idle` 或 `done` | `src/herdr_orchestrator/herdr.py` |
| Task receipt | 对任务输出前缀或 execution-root 文件的机器验收契约 | `src/herdr_orchestrator/model.py` |
| `task_verified` | Receipt 是否通过；未声明 receipt 时为 `null` | `src/herdr_orchestrator/store.py` |
| Blocked | Agent 有持续交互问题。普通 queue 不自动回答 | `src/herdr_orchestrator/model.py` |
| Resume | 人工审查后向原 blocked agent/pane 回答，不增加 attempt，不重发任务 prompt | `src/herdr_orchestrator/runner.py` |
| GC | 只回收可证明由当前 workflow 创建的 settled pane；默认 dry-run | `src/herdr_orchestrator/runner.py` |
| Runtime drift | Durable job 与实时 Herdr topology/lifecycle 不一致 | `src/herdr_orchestrator/dashboard/projector.py` |
| Correlation ID | 连接 job、receipt、事件、指标和告警的随机标识 | `src/herdr_orchestrator/store.py` |
| Manual manager | 当前 Herdr session 的临时交互式管理 agent，不具备 durable 语义 | `manager/AGENTS.md` |
| Manager Light | 把 Manager role 和 Agent lifecycle 投影到 Herdr sidebar token 的可选插件 | `plugins/manager-light/` |
| Standardized delivery | 显式触发的 Wayfinder、spec、ticket DAG、integration 与 review 流程 | `src/herdr_orchestrator/delivery.py` |
| Principal proxy | 标准化交付中处理规格内本地问题的受限 controller loop | `src/herdr_orchestrator/delivery.py` |

术语之间的关系见[系统架构](architecture.md)，持久化字段见[数据模型](../reference/data-models.md)。

## 关键源文件

| 文件 | 用途 |
| --- | --- |
| `src/herdr_orchestrator/model.py` | 核心枚举和 dataclass |
| `src/herdr_orchestrator/store.py` | Durable 状态与 receipt 字段 |
| `src/herdr_orchestrator/topology.py` | Placement 规则 |
| `manager/AGENTS.md` | 手动 Manager 边界 |
| `docs/architecture.md` | 运行与恢复语义 |
