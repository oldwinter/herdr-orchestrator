# 代码库历程
Active contributors: oldwinter, chendongdong

本页以 `v0.1.2` 标签为主要历史边界，并补充标签后的当前提交：仓库于
**2026-08-23** 初始化，到 **2026-08-25** 的 `v0.1.2` 共包含 **18 个提交**，
当前 `HEAD` `61400e5` 则包含 **19 个提交**，其中 5 个是合并提交。下文只把提交、
diff 和同期文档能够证明的变化写成事实；提交历史无法直接证明的动机，则用“可能”、
“看起来”或“似乎”表述。

## 时代一：耐久调度核心成形（2026-08-23）

**2026-08-23**，初始提交 `b4899a7` 一次建立了项目至今存续时间最长的主干：声明式 TOML workflow、确定性 coordinator、SQLite durable queue、事务内 claim、lease、attempt、重试、`dedupe_key`、receipt、受约束的 planner JSON，以及 Herdr transport adapter。初版已经把 `blocked`、`unknown` 和 timeout 与成功区分开，也明确拒绝让 planner 直接产生任意 shell command。换言之，后来功能大幅增长，但“模型提出选择，coordinator 验证并推进状态”从第一天起就没有被替换。相关真源位于 `src/herdr_orchestrator/runner.py`、`src/herdr_orchestrator/store.py` 和 `src/herdr_orchestrator/herdr.py`。

**2026-08-23** 的第二个功能提交 `4e0670f` 加入 Grok Build，并引入同一 harness 的 `replicas` 与稳定 slot name。worker 数量由五种增至六种；默认 `replicas = 1` 仍保持同 harness 串行，需要时才扩大并发。这看起来是在不改变 durable queue 语义的前提下，先解决 worker 池扩展问题。当天仓库由初始的 32 个文件、3,019 行增长到 38 个文件、3,314 行。

## 时代二：从“派发器”扩展为分层控制面（2026-08-24）

**2026-08-24**，`0e55a06` 是第一次大规模扩写：58 个文件合计增加 7,478 行、删除 100 行。它把六种 harness 的紧凑 TOML catalog 与完整 Markdown profile 分开，只有被选中的完整 profile 才在 dispatch 前加载；同时把 controller 与 worker 的选择拆开。对应真源是 `profiles/harnesses/`、`src/herdr_orchestrator/catalog.py` 和 `src/herdr_orchestrator/selection.py`。从 diff 看，这可能是为了避免主控预加载所有 worker 的长上下文，但历史本身不能证明性能或上下文成本是唯一动机。

同一个 **2026-08-24** 提交还加入显式 opt-in 的 standardized delivery：Wayfinder 决策消雾、spec、依赖有序 ticket DAG、隔离 worktree、双轴 review 与有界 repair。它没有取代普通 durable queue，而是建立了第二条、不混用 job 状态的运行面；实现集中在 `src/herdr_orchestrator/delivery.py` 及相邻的 delivery protocol、tracker 模块，契约记录在 `docs/standardized-delivery.md`。

## 时代三：执行位置与可观测性成为一等概念（2026-08-24）

**2026-08-24**，`68044cf` 增加 topology-aware execution，把“谁执行”与“在哪里执行”分离。任务可落在共享 checkout 的独立 tab、批次共享 tab 的独立 pane，或 Herdr 原生 worktree；显式覆盖、worker 默认、确定性读写规则和受限 controller JSON 依次决定 placement。该提交增加 2,530 行、删除 128 行，是 coordinator/Herdr 边界的一次主要改写，但保留了既有 queue、lease 和 retry 主线。相关实现位于 `src/herdr_orchestrator/topology.py`、`src/herdr_orchestrator/herdr_layout.py` 和 `src/herdr_orchestrator/runner.py`。

**2026-08-24**，`76d01ce` 再增加 2,507 行，加入本地实时 Dashboard。它从 SQLite 与 Herdr 白名单字段生成只读 snapshot，通过 loopback HTTP 和 SSE 展示 queue、attention、runtime drift、topology 与 receipt timeline；不读取 prompt、环境变量或 pane output，也不提供修改状态的 POST 接口。将它放在 coordinator 旁路而不是调度路径中，似乎是为了让观测故障不影响 lease 和任务推进。边界见 `src/herdr_orchestrator/dashboard/` 与 `docs/dashboard.md`。

## 时代四：从源码仓库走向可安装、可发布产品（2026-08-24）

**2026-08-24**，`8c8e330` 加入 npm 一键安装器和可独立分发的 Skill。`bin/herdr-orchestrator.mjs` 开始负责 install、upgrade、doctor、uninstall 与 runtime 转发；manifest hash、托管根目录和 symlink 检查用于避免覆盖用户文件。该提交增加 1,141 行，使项目不再只依赖源码 checkout。

**2026-08-24**，`cb85dca` 建立从 `main` 发布 npm 版本的 CI：registry version gate 先判断版本是否已存在，缺失版本才通过 npm Trusted Publishing 发布，且不引入长期 npm token。到当天最后一个非合并里程碑，仓库已达到 100 个文件、17,066 行、18 个 Python 测试文件和 24 个包内 Python 模块。

## 时代五：展示层升级，完成语义被重新硬化（2026-08-25）

**2026-08-25**，`a6b2560` 把 Dashboard 的层级 topology 展示升级为可平移、缩放的 Cytoscape Canvas compound graph，结构从 `workspace → tab → pane → agent` 扩展为 `project → worktree/workspace → tab → pane`。这是历史中最接近“替换”的用户界面变化，但它特意保留 Snapshot v1 的 `topology.workspaces`，以 additive `topology.projects` 扩展协议，并保留屏幕阅读器可读的 DOM tree；因此旧消费者并未被强制迁移。

**2026-08-25**，`2816491` 以 3,414 行新增、184 行删除重写多 harness 派发的完成判定。此前的 settled lifecycle 被拆成 `agent_settled` 与 `task_verified`：声明 output-prefix 或 file receipt 的任务必须通过内容级机器契约才能成功；同时加入 `run-until-idle`、显式 retry、受所有权校验的 dry-run GC、跨 harness fatal runtime signal 与更清晰的 batch/queue 计数。这里不是把 `idle`/`done` 废弃，而是把它从“任务完成”收窄为“agent 已稳定结束当前 turn”。

**2026-08-25**，`fa0b310` 继续硬化恢复与 receipt，增加 1,997 行、删除 93 行。output receipt 必须来自当前 turn 的新增输出，不能仅是 prompt echo；file receipt 必须在当前 turn 新建或变化；prompt command timeout 后要复查 `state_change_seq`，以区分已接受但等待超时和根本未接受。普通 queue 还获得显式 `resume --response-file`：它复用原 agent、pane 和 attempt，不重发任务 prompt，且再次提问仍保持 `blocked`。这些变化说明项目在 **2026-08-25** 已从“能派发”转向“能证明派发过程与恢复路径没有被误判”。更完整的实战经验见[运行时经验](background/runtime-lessons.md)。

## 时代六：仓库级门禁与 v0.1.2 自动化边界（2026-08-25）

**2026-08-25**，`4d3bc36` 是截至 `v0.1.2` 改动面最广的提交：71 个文件增加 4,649 行、删除 1,120 行，并将版本提升到 `v0.1.1`。它补齐 uv lock、lint、coverage、flaky-test detection、security review、build metrics、profiling、import boundary、devcontainer、CODEOWNERS、贡献与安全文档，以及结构化 observability。CI 的普通测试与质量门禁回到 GitHub-hosted runner，registry version plan 保留在专用 self-hosted runner，而 OIDC publish 继续使用 GitHub-hosted runner；这很可能是在隔离发布权限、可信运行环境和日常验证成本，但 commit 不能单独证明全部取舍。

**2026-08-25**，`b7c90e9` 将 Python 与 npm 包同步提升到 `v0.1.2`，并为六种 harness 固定各自的最高自动化启动参数。planner 和 task packet 不能注入或覆盖这些参数；Claude 首次 workspace trust 也只在 detection output 同时匹配稳定标记与预期 execution root 时自动确认，其他登录、secret、approval 与需求问题仍不自动回答。该提交新增专门的 startup automation 测试，表明“减少本地确认”与“扩大任务授权”被刻意保持为两件事。`v0.1.2` 时仓库共有 137 个文件、27,339 行、20 个 Python 测试文件和 26 个包内 Python 模块。

## 时代七：拓扑投影被抽成可独立验证的纯模块（2026-08-26）

**2026-08-26**，`61400e5` 把 Dashboard 的 graph normalization、稳定 ID、双签名和
preset 坐标从 `dashboard.js` 抽到无 DOM 的
`src/herdr_orchestrator/dashboard/static/topology.js`，并新增
`tests/test_topology_js.py` 通过 Node 直接验证 compound graph、fallback、状态 class 和
确定性布局。该提交同时修正 `justfile` 的 variadic 参数转发，确保带多个 harness 或额外
CLI 参数的稳定 recipe 不会丢失参数。它没有改变 Snapshot v1，也没有把 Dashboard 变成
写控制面；当前树达到 139 个跟踪条目、27,676 行、21 个 Python 测试文件和 26 个包内
Python 模块。

## 最长存续主线

从 **2026-08-23** 到 **2026-08-26**，以下能力贯穿当前 19 个提交，经历扩展或硬化但没有被替换：

- **确定性状态所有权**：SQLite、事务 claim、lease、attempt 和 dedupe 始终由 coordinator/store 管理，而不是交给模型。
- **Planner 受限输入**：planner 从第一天起只产生受 schema 校验的结构化任务；后来 catalog、router 和 topology decision 延续了同一原则。
- **Herdr 是 runtime 而非主控**：Herdr 持续负责 PTY、pane、tab、workspace 与 agent lifecycle，业务状态仍由 Python 控制面解释。
- **失败不是成功**：`blocked`、`unknown`、timeout 和协议错误自 **2026-08-23** 起就不算成功；**2026-08-25** 又用 current-turn receipt 与 prompt-acceptance evidence 收紧了证明标准。
- **默认不做外部副作用**：不自动 push、merge、发布任务产物、删除 worktree 或触碰生产环境的边界从初始 README 延续到 `v0.1.2`。

这些持续不变的原则解释了为什么后来能叠加 topology、Dashboard、安装器和 standardized delivery，而不用替换 durable queue 核心。架构关系见[架构总览](overview/architecture.md)，取舍脉络见[设计决策](background/design-decisions.md)。

## 增长轨迹

| 日期与里程碑 | 提交数快照 | 文件 | 当前树文本行 | Python 测试文件 | 包内 Python 模块 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-08-23，初始化 `b4899a7` | 1 | 32 | 3,019 | 6 | 10 |
| 2026-08-24，发布流水线 `cb85dca` | 10 | 100 | 17,066 | 18 | 24 |
| 2026-08-25，可信派发 `2816491` | 13 | 103 | 21,335 | 18 | 24 |
| 2026-08-25，恢复硬化 `fa0b310` | 16 | 106 | 23,302 | 18 | 24 |
| 2026-08-25，`v0.1.2` | 18 | 137 | 27,339 | 20 | 26 |
| 2026-08-26，当前 `61400e5` | 19 | 139 | 27,676 | 21 | 26 |

表中的“当前树文本行”来自空树到对应 commit 的 Git diff 统计，不是把每次 commit 的新增行机械相加；合并提交计入提交数，但没有重复计算文件内容。更完整的规模切面见[数字概览](by-the-numbers.md)。

## 关于替换、弃用与兼容性

截至 **2026-08-26** 的 `61400e5`，历史中未发现实际被标记为 deprecated 的功能，也没有已跟踪路径被删除或重命名；不应为这段短历史编造弃用故事。能够确认的是两类渐进替换：

1. **2026-08-25** 的 Dashboard topology 从简单层级展示升级为 Canvas，但 Snapshot v1 与可访问 DOM tree 被保留。
2. **2026-08-25** 的任务成功语义从仅依赖 settled lifecycle，硬化为可选但严格的 task receipt；未声明 receipt 的兼容任务仍保留 `task_verified = null`，没有被强制淘汰。

因此，这一阶段更准确的描述是“快速扩张后连续硬化”，而不是“旧产品被新产品取代”。

## 史料入口

- 初始定位与当前使用面：`README.md`
- 状态机、拓扑和恢复语义：`docs/architecture.md`
- 标准化交付契约：`docs/standardized-delivery.md`
- Dashboard 演进后的边界：`docs/dashboard.md`
- 默认 workflow：`workflows/multi-harness.toml`
- 设计背景：[设计决策](background/design-decisions.md)
- 运行证据与教训：[运行时经验](background/runtime-lessons.md)
