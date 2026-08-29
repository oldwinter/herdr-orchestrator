# 关键设计取舍
Active contributors: oldwinter, chendongdong

本页不是补写的 ADR。每项决定都能从当前实现、公开文档或 `origin/main` 历史核验；没有证据的
历史动机不作为事实。这里关注决定、直接后果与维护代价。

## 1. 确定性控制面拥有状态推进权

**决定**

- `src/herdr_orchestrator/runner.py::Coordinator` 决定 planner、placement、claim、wave、
  drain、resume 和 GC 何时发生；
- `src/herdr_orchestrator/store.py::Store` 用 SQLite 保存 job、dedupe key、attempt、lease 和
  append-only receipt；
- Planner/router/topology/reviewer 只能提交严格、有界的 artifact，不能直接改 queue state。

```mermaid
flowchart LR
    模型[模型决策] --> 校验[Schema + allowlist]
    校验 --> Queue[(SQLite)]
    Queue --> Claim[事务 claim + lease]
    Claim --> Dispatch[Coordinator dispatch]
    Dispatch --> Outcome[结构化 outcome]
    Outcome --> Queue
```

**后果与代价**

- Coordinator 重启后可从 lease/attempt 恢复，不依赖模型记忆或 terminal 标题；
- `(workflow, dedupe_key)` 防止重复入队，但外部副作用仍需自己的幂等键；
- 状态与 migration 成为长期接口，修改时必须同步 model/store/CLI/Dashboard/tests；
- 灵活性只存在于受控 decision seam，确定性代码承担更多校验与错误分类复杂度。

参见 [Coordinator 与队列](../systems/coordinator-and-queue.md)、
[Durable execution](../features/durable-execution.md)。

## 2. Herdr 是 terminal runtime，不是推理主控

**决定**

`src/herdr_orchestrator/herdr.py` 把 Herdr 当作结构化 transport：provision topology、启动或
复用 agent、提交输入、读取 lifecycle sequence、验证 pane/workspace/cwd 并等待结果。
Worker 选择、lease、retry 和成功判定仍属于 coordinator/store。

**后果与代价**

- 可以利用 Herdr 的持久 PTY、tab/pane/worktree 和 detach/reattach，而不把 durable 状态机
  放进 terminal runtime；
- Herdr server 重启、睡眠、网络或 harness session 丢失仍可能触发 lease reclaim 和重复执行；
- Terminal output 只用于有界诊断、fatal signal 和显式 output receipt，不是通用真相；
- Herdr CLI 的 JSON、`state_change_seq` 和 identity 成为必须维护的 transport contract。

参见 [Herdr runtime](../systems/herdr-runtime.md)。

## 3. 普通 queue、标准化交付和手动 manager 是三条不同运行面

**决定**

| 运行面 | 入口 | 拥有的语义 |
| --- | --- | --- |
| Durable queue | `enqueue/run/retry/resume/gc` | SQLite、lease、attempt、dedupe、receipt |
| 标准化交付 | 显式 `deliver` 或指定 Skill/触发语 | spec、ticket DAG、worktree、review、repair、integration |
| 手动 manager | `herdr-manager` / `manager` | 当前 Herdr session 的交互式观察与最小显式操作 |

Manager-light 只为 sidebar 投影 manager/blocked/working/idle/unknown token，不构成第四个
调度面。

**后果与代价**

- 普通实现或 review 不会隐式升级为创建 tracker、branch 和多 worktree 的长流程；
- Manual manager 不伪造 durable 成功、重试或跨重启恢复；
- 三条运行面可共享 Herdr、catalog 或 policy，但不能互换状态、receipt 或授权；
- 维护者必须明确选择入口，文档也必须避免把 manager 的 idle 当 queue success。

参见 [标准化交付](../systems/standardized-delivery.md)和
[CLI 机器契约](../api/cli-contracts.md)。

## 4. Harness catalog 分两级加载

**决定**

- `profiles/harnesses/*.toml` 是 controller 可见的 compact metadata；
- Workflow workers、planner worker pool 与 CLI override 逐层收窄候选；
- Job 被选中后才加载对应 `profiles/harnesses/*.md` full context；
- Profile path、字段、大小与 harness identity fail closed。

**后果与代价**

- 未选 harness 的完整上下文不会进入当前 turn；
- TOML 的路由定位与 Markdown 的执行契约职责不同，修改时要选择正确真源；
- [推断] 这会降低 controller context 体积与无关 profile 干扰，但仓库没有把该动机记录为 ADR；
- Compact metadata 本身成为路由质量关键接口。

参见 [Harness catalog 与路由](../systems/catalog-and-routing.md)、
[Harness profile](../primitives/harness-profiles.md)。

## 5. Harness selection 与 placement 分离

**决定**

Harness 回答“谁做”，placement 回答“在哪里做”。Placement 优先级为：

```text
任务显式 override
  → worker 默认值
  → 确定性读写规则
  → 模糊任务的受限 controller JSON
  → PlacementTarget 校验
```

| Placement | 隔离 | 生命周期 |
| --- | --- | --- |
| `pane` | 同一 wave batch tab 中独立 pane，共享 checkout | 适合只读并行；GC 可按 ownership 关 pane |
| `tab` | 独立 full-size tab，共享 checkout | 保留终端空间；没有 checkout 隔离 |
| `worktree` | 独立 branch、checkout、Herdr workspace | Retry 可恢复；普通 GC 不删除或 merge |

**后果与代价**

- 新增 harness 不需要重新定义 topology；
- 写任务可隔离 checkout，只读任务不必总创建 worktree；
- 模糊任务可能增加一次受限 controller turn；
- Worktree 保留有利于恢复与审查，但增加磁盘占用和人工生命周期管理。

参见 [拓扑感知派发](../features/topology-aware-dispatch.md)、
[Placement 与 worktree](../primitives/placement-and-worktrees.md)。

## 6. 任务成功需要证据梯，而不是单一状态词

**决定**

```mermaid
flowchart LR
    P[Provisioned] --> R[Interactive ready]
    R --> T[Turn observed]
    T --> S[Stable settlement]
    S --> V[可选 receipt verified]
```

`agent_settled` 与 `task_verified` 独立。Output receipt 只接受当前 turn 新增且不与 prompt
歧义的行；file receipt 必须位于 execution root 并在当前 turn 新建或改变。

**后果与代价**

- Pane 存在、ready、单次 idle 或历史输出都不能被当作任务成功；
- 没声明 receipt 的兼容 job 仍可在 settled 且无 fatal signal 时成功，但
  `task_verified=null`；
- Receipt 只证明窄机器契约，不替代规格或代码 review；
- 更严格的 freshness 会暴露旧假阳性，并要求任务作者设计稳定 receipt。

参见 [收据与恢复](../features/receipts-and-recovery.md)。

## 7. Timeout 先对账，blocked 默认保留人工边界

**决定**

Prompt command timeout 后先重新读取 sequence/state：sequence 已前进就继续原 turn，未前进
才返回 acceptance timeout。普通 queue 的 persistent blocked 不自动回答，只接受显式
`resume --response-file`，并保持原 agent/pane/attempt。

**后果与代价**

- 避免“调用方超时但任务已开始”时盲目重发造成重复副作用；
- Startup transient blocked 与真实任务 blocked 分开处理；
- 普通 queue 的自动化程度低于猜答式系统，但不会暗中越过用户决策；
- 标准化交付的 principal proxy 必须留在独立 opt-in authority 中。

参见 [运行时经验](runtime-lessons.md)。

## 8. Dashboard 是 loopback-only、只读旁路

**决定**

SQLite read-only observer 和 Herdr whitelist observer 经 `RuntimeProjector` 生成完整 snapshot，
再由内存 feed 通过 HTTP/SSE 发送。Dashboard 不 claim、retry、resume、focus，也不读取
prompt 或 terminal output。

**后果与代价**

- UI 故障、断线或 projector 异常不改变 coordinator/lease/receipt；
- 可关联 `job → agent → pane → tab → workspace` 并显示 drift/attention；
- SSE 是最新值流，event ID 不是 durable log；
- UI 无权修复 drift；操作仍回到 CLI/Herdr；
- 无认证模型只适用于 loopback、只读和字段白名单。

参见 [本地 Dashboard](../systems/dashboard.md)和
[Dashboard HTTP 与 SSE](../api/dashboard-http-sse.md)。

## 9. Observability 默认本地、脱敏、fail-soft

**决定**

Coordinator 用 correlation ID 关联 job/attempt/receipt 和本地 JSONL。敏感字段在持久化与外发
前统一 sanitize；Sentry、PostHog、HTTPS webhook 默认关闭。Telemetry 故障不拥有 queue
状态。

**后果与代价**

- 默认不要求 SaaS，适合离线或敏感仓库；
- 不保存完整 terminal transcript，错误摘要保持有界；
- 本地文件仍需 retention 和 OS 权限策略；
- 跨机器聚合与长期保留需额外显式配置。

参见 [可观测性与 Attention](../features/observability-and-attention.md)。

## 10. Runtime 保持最小包依赖

**决定**

`pyproject.toml` 的 Python runtime dependencies 为空；根 `package.json` 也没有 npm
dependencies。SQLite、HTTP、SSE、TOML、subprocess 与并发均由 Python/Node 标准库或仓库内
代码承担。Dashboard 的 Cytoscape.js 是带许可证的 vendored static asset。

独立 `herdr-manager` 包只有一个依赖：`herdr-orchestrator ^0.1.6`，以复用实现而不是复制
manager 逻辑。

**后果与代价**

- Runtime dependency resolver 和供应链面较小；
- “零 package dependency”不等于零系统前提：Herdr、Git、harness CLI、Python/Node 仍需安装；
- 标准库实现和 vendored asset 的维护/测试责任留在仓库；
- Manager 薄包版本与运行包兼容范围需要独立协调。

参见 [依赖参考](../reference/dependencies.md)。

## 11. 项目安装使用 manifest ownership，而不是覆盖式复制

**决定**

`bin/herdr-orchestrator.mjs` 将受管范围限制在项目本地 allowlist，记录每个文件 SHA-256，
拒绝 path escape/symlink，并保留用户修改。Git-local exclude 使用带 marker block，不修改
tracked `.gitignore`。

**后果与代价**

- 重装、升级和卸载不会静默覆盖用户修改；
- 内容相同但由其他工具安装的 Skill 可复用但不被接管；
- Doctor 可以报告 missing、modified 与 version skew；
- 多文件 reconciliation 不是文件系统事务，异常中断可能留下 partial installation，需要
  可重复 install 与 doctor 恢复。

参见 [安装与分发](../systems/installation-and-distribution.md)和
[部署](../deployment.md)。

## 12. Manager-light 只拥有一个可验证配置区块

**决定**

`plugins/manager-light/configure.mjs` 只管理一个带 begin/end marker 的
`[ui.sidebar.agents]` block 和 canonical package plugin link。候选配置先通过
`herdr config check` 再原子 rename；区块外已有配置原样保留。

**后果与代价**

- 与外部 Agent row、修改过的 owned block 或同名外部 plugin 冲突时 fail closed；
- 安装/卸载可验证 ownership，不需要通用 Herdr 配置管理器；
- 蓝色 manager token 是 best-effort UI metadata，不影响 manager process；
- 配置跨文件/plugin 操作仍需 rollback 路径和大量分支测试。

参见 [安全](../security.md)和 [部署](../deployment.md)。

## 13. 发布把可信计划与 OIDC 写权限分开

**决定**

PR/test 使用 GitHub-hosted runner；通过测试的可信 `main` 才在专属 self-hosted runner 查询
两个 npm 包的 registry 版本；实际 publish 回到 GitHub-hosted runner，通过 Environment
`npm` 和 OIDC 发布。

**后果与代价**

- 不可信 contributor code 不进入 persistent runner；
- Registry 查询失败不会被解释为“版本缺失”；
- npm Trusted Publishing 无长期 token；
- 双包可独立 no-op/发布，但版本依赖与部分成功恢复更复杂；
- Publish job 需要 `contents: write` 创建 GitHub Release，因此权限文档和测试必须跟随真实
  workflow。

参见 [部署与发布](../deployment.md)。

## 14. 标准化交付成功止于隔离 integration branch

**决定**

显式 delivery 按 `wayfinder? → spec/ticket DAG → frontier worktrees → integration →
Standards ∥ Spec review → bounded repair` 推进。Principal proxy 只回答 accepted spec
内本地问题；secret/production 必须升级。

**后果与代价**

- Worker 必须提交 clean commit 和 criterion evidence，coordinator merge 后才关闭 ticket；
- Review 与 repair 次数有界，不形成递归派发；
- 成功不 push、不创建 PR、不 merge 用户 branch、不 deploy；
- 失败保留 artifact/worktree 便于恢复，但需要明确 retention。

参见 [标准化交付](../systems/standardized-delivery.md)和
[交付 Artifact](../primitives/delivery-artifacts.md)。

## 总结

项目反复采用同一模式：把模型灵活性放在严格 decision seam，把 durable 状态、ownership、
receipt、权限与恢复留给确定性代码。收益是可恢复、可检查和 fail closed；代价是 schema、
migration、artifact、CLI、安装 manifest、配置 marker 和运行协议都必须当作长期接口维护。
