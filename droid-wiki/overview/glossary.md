# 术语表
Active contributors: oldwinter, chendongdong

本页统一 Herdr Orchestrator 在代码、CLI 和运行时输出中使用的词汇。状态和枚举的真源是 `src/herdr_orchestrator/model.py`。

| 术语 | 含义 |
| --- | --- |
| Agent | Herdr 中一个具名的交互式 harness 进程，绑定 pane、cwd 和 lifecycle state。 |
| Attempt | Job 被成功 claim 的次数。Lease 过期后重新 claim 会增加 attempt；`resume` 不增加。 |
| Blocked | Agent 已进入需要输入的终态。普通 queue 只允许显式 `resume`，标准化交付可使用有界 principal proxy。 |
| Compact catalog | `profiles/harnesses/*.toml` 中供 controller 选择 worker 的紧凑能力元数据。 |
| Controller | 执行 planner、worker router、topology decision 或交付裁决的 harness；它不是队列状态真源。 |
| Correlation ID | 每次 dispatch attempt 的随机 ID，用于关联 job、receipt 与本地可观测性记录。 |
| Dedupe key | Workflow 范围内的稳定任务标识，SQLite 对 `(workflow, dedupe_key)` 建唯一约束。 |
| Durable queue | `src/herdr_orchestrator/store.py` 管理的 SQLite job、lease、attempt 和 receipt 状态。 |
| Execution root | Agent 实际运行目录。Pane/tab 使用 workflow workspace，worktree 使用独立 checkout。 |
| Full profile | 被选中 harness 的 Markdown 上下文，只在 dispatch 前由 `src/herdr_orchestrator/catalog.py` 动态加载。 |
| Harness | Droid、Grok Build、Codex、pi、Claude Code 或 Hermes 之一。 |
| Herdr | 提供 PTY、workspace、tab、pane、worktree 和 agent lifecycle 的本地 terminal runtime。 |
| Job | 普通 durable queue 中一个带 title、prompt、harness、placement、attempt budget 和可选 receipt 的工作项。 |
| Lease | Running job 在 SQLite 中持有的有界执行权。Coordinator 崩溃后，过期 lease 可被重新 claim。 |
| Pane placement | 同一 `run_once` 批次共享 tab，但每个任务使用独立 pane 和 agent。 |
| Principal proxy | 仅在显式标准化交付中代用户回答规格内、本地、可逆问题的 controller；secret 和 production 必须升级。 |
| Receipt | Job attempt 的持久生命周期记录，或任务声明的 output-prefix/file 机器验收契约。 |
| Replica | 同一 harness 可同时占用的 slot 数。默认 1。 |
| Settled | Agent 在当前 turn 后稳定处于 `idle` 或 `done`；不代表任务内容正确。 |
| Standardized delivery | 显式 opt-in 的 spec、ticket DAG、隔离 worktree、双轴 review 与 repair 流程。 |
| Task verified | 声明的 output-prefix 或 file receipt 已通过。未声明时值为 `null`。 |
| Topology | Task 在 Herdr 中的执行位置，即 tab、pane 或原生 worktree。 |
| Worktree placement | Herdr 创建或打开独立 Git checkout、branch 和 workspace；普通 queue 不自动 merge 或删除。 |
| Worker | 实际执行 task packet 的 harness。Controller 和 worker 可分别选择。 |

更多领域对象见[数据模型参考](../reference/data-models.md)，运行状态转换见[任务与收据](../primitives/jobs-and-receipts.md)，执行位置见[Placement 与 worktree](../primitives/placement-and-worktrees.md)。

## 关键源文件

| 文件 | 作用 |
| --- | --- |
| `src/herdr_orchestrator/model.py` | Harness、JobState、AgentState、PlacementTarget 和 dataclass |
| `src/herdr_orchestrator/store.py` | Attempt、lease、job 与 receipt 的持久语义 |
| `src/herdr_orchestrator/delivery_protocol.py` | 标准化交付 artifact 词汇 |
| `docs/runtime-troubleshooting.md` | Provisioned、ready、turn observed 与 settled 的证据层次 |
