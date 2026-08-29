# 代码库历程

本页以 `origin/main` 的提交、标签与路径历史为史料。当前边界是
**2026-08-28** 的 `7291093` / `v0.1.6`：从 **2026-08-23** 的首个提交开始，
主分支六天内形成 26 个可达提交。提交能证明“发生了什么”，不一定能证明“为什么”；
动机没有写入 commit 或同期文档时，下文会明确保留判断。

## 第一阶段：耐久调度内核（2026-08-23）

**2026-08-23**，初始提交 `b4899a7` 建立了此后持续存在的控制面：
声明式 TOML workflow、SQLite durable queue、事务 claim、lease、attempt、重试、
`dedupe_key`、receipt、受约束的 planner JSON，以及 Herdr transport。核心实现从
`src/herdr_orchestrator/runner.py`、`src/herdr_orchestrator/store.py`、
`src/herdr_orchestrator/herdr.py` 和 `src/herdr_orchestrator/config.py` 起步。
Herdr 负责终端运行时，coordinator 拥有业务状态的分工从这一天起没有被推翻。

**2026-08-23**，`4e0670f` 将 Grok 加入 worker，并为同一种 harness 增加
`replicas` 与稳定 slot；默认 replica 数仍是 1。到当天结束，按本页统一排除口径，
代码树从初始提交的 32 个文本文件、3,019 行增长到 38 个文本文件、3,314 行。

## 第二阶段：分层控制面、拓扑与可观测性（2026-08-24）

**2026-08-24**，`0e55a06` 用 58 个变更文件、7,478 行新增和 100 行删除，
同时引入两级 harness catalog 与显式 opt-in 的 standardized delivery。
紧凑 catalog 位于 `profiles/harnesses/*.toml`，完整执行上下文位于
`profiles/harnesses/*.md`；派发逻辑集中在
`src/herdr_orchestrator/catalog.py`、`src/herdr_orchestrator/selection.py`，
交付实现集中在 `src/herdr_orchestrator/delivery.py`。这个提交把普通 durable queue
与标准化交付作为两个运行面保留，而不是让后者替代前者。

**2026-08-24**，`68044cf` 增加 topology-aware execution，将 worker 选择与
tab、pane、worktree placement 分开；对应实现是
`src/herdr_orchestrator/topology.py` 与
`src/herdr_orchestrator/herdr_layout.py`。同日 `76d01ce` 增加 loopback、
只读、SSE 驱动的 Dashboard，代码位于
`src/herdr_orchestrator/dashboard/server.py`、
`src/herdr_orchestrator/dashboard/projector.py` 和
`src/herdr_orchestrator/dashboard/static/dashboard.js`。

**2026-08-24**，`8c8e330` 加入依赖较薄的 npm installer
`bin/herdr-orchestrator.mjs`；`cb85dca` 随后建立 main-only registry version gate
与 npm Trusted Publishing。项目从本地源码入口扩展为可安装、可发布的产品面。

## 第三阶段：从“agent 停下”到“任务可验证”（2026-08-25）

**2026-08-25**，`a6b2560` 将 Dashboard topology 升级为 Cytoscape Canvas，
同时以 additive 的 `topology.projects` 保留既有 `topology.workspaces` 投影；
这是界面重写，但不是 Snapshot v1 的破坏式替换。

**2026-08-25**，`2816491` 以 3,414 行新增、184 行删除重写派发完成判定：
`agent_settled` 与 `task_verified` 被拆开，声明 output/file receipt 的任务必须满足
当前任务的机器契约；同一提交加入 `run-until-idle`、显式 retry 与有所有权约束的 GC。
**2026-08-25**，`fa0b310` 又用 1,997 行新增、93 行删除收紧恢复：
receipt 必须来自当前 turn，prompt timeout 需要接受证据，普通 queue 的 blocked job
只能通过显式 response file 恢复。

**2026-08-25**，`4d3bc36` 横跨 71 个文件，加入质量、安全、治理、observability、
import boundary 和可复现开发门禁；`b7c90e9` 则固定六种 harness 的最高自动化启动参数，
并发布 **2026-08-25 的 `v0.1.2`**。这一天的主线更适合描述为“证明和恢复语义硬化”，
而不是新调度器替换旧调度器。

## 第四阶段：可独立测试的拓扑与中文知识面（2026-08-26）

**2026-08-26**，`61400e5` 把无 DOM 的图投影抽到
`src/herdr_orchestrator/dashboard/static/topology.js`，并由
`tests/test_topology_js.py` 通过 Node 验证稳定 ID、compound graph、fallback 与布局；
同一提交修正 `justfile` 的 variadic 参数转发。Snapshot 协议和只读边界保持不变。

**2026-08-26**，`4637ab8` 将中文 Wiki、海报与双语字幕作为 52 个路径的文档里程碑
并入 Git。由于本页的增长统计排除 `droid-wiki/**`，这个提交在代码规模表中不会制造
“实现突然增长”的假象。

## 第五阶段：交互式 Manual Manager（2026-08-27）

**2026-08-27**，`5aa115e` 增加固定策略目录
`manager/AGENTS.md` 与 `manager/CLAUDE.md`，形成 coordinator 之外的交互式
Manual Manager：它只管理当前 Herdr session，不模拟 queue、lease、retry 或 receipt。

**2026-08-27**，`f38daac` 简化源码 checkout 与安装后的 manager 命令，并发布
**`v0.1.3`**；`1f344b9` 将未指定 harness 时的候选顺序改为
Grok、Codex、Claude，并发布 **`v0.1.4`**。提交历史能证明顺序变化，却没有记录
“为什么 Grok 必须排在首位”的完整产品动机，因此这里不做进一步推断。

## 第六阶段：Manager Light 与一条命令分发（2026-08-28）

**2026-08-28**，`2b9ab77` 增加
`plugins/manager-light/configure.mjs`、
`plugins/manager-light/projection.mjs` 和
`plugins/manager-light/herdr-plugin.toml`，以显式 install/status/uninstall 管理
包拥有的 Herdr 插件与 sidebar 投影，并发布 **`v0.1.5`**。它是可选的展示伴侣，
没有接管 durable queue。

**2026-08-28**，`34ba977` 加入 Factory overview video 制作资产与 mock prompt；
这些 `.factory/video/**` 资产在代码规模统计中被排除。随后 `7291093` 新增薄包装入口
`packages/herdr-manager/bin/herdr-manager.mjs`，让 `npx herdr-manager` 以固定 argv
转发到 `herdr-orchestrator manager`，并把双包 release plan 纳入
`scripts/npm-release-plan.mjs`。该提交发布 **2026-08-28 的 `v0.1.6`**。

## 从首日延续至今的主线

- **2026-08-23—2026-08-28：确定性状态所有权。** claim、lease、attempt、
  retry 与 receipt 始终由 `src/herdr_orchestrator/store.py` 和 coordinator 推进，
  未交给 planner 任意改写。
- **2026-08-23—2026-08-28：结构化、受校验的模型输出。** 初始 planner JSON，
  到后来的 catalog selection、topology decision 与 delivery protocol，都延续
  “模型提议、确定性代码验证”的边界。
- **2026-08-23—2026-08-28：Herdr 是 runtime。** PTY、pane、tab、workspace
  与 agent lifecycle 属于 Herdr；`src/herdr_orchestrator/herdr.py` 负责适配，
  业务状态仍留在 Python 控制面。
- **2026-08-23—2026-08-28：失败不伪装成成功。** blocked、unknown 与 timeout
  从首日就不是成功；**2026-08-25** 又以 current-turn receipt 和
  `task_verified` 收紧证据。
- **2026-08-24—2026-08-28：多运行面并存。** durable queue、standardized
  delivery、只读 Dashboard 与 Manual Manager 各有状态边界，后加功能没有吞并旧核心。

## 重写、替代与弃用证据

截至 **2026-08-28** 的 `7291093`，`origin/main` 的路径历史中没有删除或重命名记录，
一方当前源码也没有显式 deprecated/deprecation 标记。历史搜索命中的 “deprecated”
只来自被排除的锁文件或 vendored 第三方内容。因此，没有证据支持编造正式弃用清单。

可以确认的替代/重写只有渐进式语义变化：

1. **2026-08-25**，Canvas 替代早期 topology 的主要视觉表达，但保留旧投影与可访问
   DOM，而不是强迫 Snapshot 消费者迁移。
2. **2026-08-25**，任务成功从宽泛的 settled 推断收紧为可选但严格的 receipt 验证；
   agent settled 仍存在，只是不再自动等价于任务完成。
3. **2026-08-26**，图规范化从
   `src/herdr_orchestrator/dashboard/static/dashboard.js` 抽到
   `src/herdr_orchestrator/dashboard/static/topology.js`，属于可测试性重构，
   不是功能弃用。
4. **2026-08-27—2026-08-28**，Manual Manager、Manager Light 与
   `packages/herdr-manager/bin/herdr-manager.mjs` 增加交互式入口，但明确没有替代
   durable queue 的无人值守恢复语义。

## 增长轨迹

下表对每个 commit/tag 的 Git tree 重新计数，并统一排除 `droid-wiki/**`、
`.factory/video/**`、锁文件、`.secrets.baseline`、`docs/generated/**`、
`security-findings.json` 与
`src/herdr_orchestrator/dashboard/static/cytoscape.min.js`；符号链接也不计作文本文件。

| 日期与里程碑 | 文本文件 | 物理 LOC | Python 测试文件 | 包内 Python 模块 |
| --- | ---: | ---: | ---: | ---: |
| 2026-08-23，初始 `b4899a7` | 32 | 3,019 | 6 | 10 |
| 2026-08-24，发布流水线 `cb85dca` | 97 | 17,045 | 18 | 24 |
| 2026-08-25，`v0.1.2` | 129 | 25,466 | 20 | 26 |
| 2026-08-26，可测试拓扑 `61400e5` | 131 | 25,803 | 21 | 26 |
| 2026-08-27，`v0.1.3` | 133 | 26,295 | 21 | 26 |
| 2026-08-27，`v0.1.4` | 133 | 26,294 | 21 | 26 |
| 2026-08-28，`v0.1.5` | 138 | 27,751 | 22 | 26 |
| 2026-08-28，`v0.1.6` / `7291093` | 143 | 28,256 | 22 | 26 |

从首个提交到 `v0.1.6`，文本文件增加约 **347%**，物理 LOC 增加约 **836%**，
Python 测试文件从 6 个增至 22 个，包内模块从 10 个增至 26 个。更细的语言、churn
与复杂度口径见[数字中的代码库](by-the-numbers.md)，状态边界见
[架构总览](overview/architecture.md)。

## 史料入口

- 当前定位：`README.md`
- 状态机与恢复：`docs/architecture.md`
- 分发与 Manual Manager：`docs/installation.md`
- 默认工作流：`workflows/multi-harness.toml`
- 设计背景：[设计决策](background/design-decisions.md)
- 运行经验：[运行时经验](background/runtime-lessons.md)
