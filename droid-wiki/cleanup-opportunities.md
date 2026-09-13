# 清理机会
Active contributors: oldwinter, chendongdong

本页记录**能由当前代码、配置、测试或文档直接证明，但尚未形成已编号工作项**的维护机会。它不是缺陷清单，也不假定存在未公开 issue；每项都明确当前风险、保留现状的取舍和适合处理的触发条件。

仓库的 `scripts/check_repository.py` 会拒绝没有 issue 与 owner 的行内债务标记，因此“此类标记很少或为零”只能说明没有无主便签，不能推出没有技术债。完整边界见[安全](security.md)，结构背景见[系统架构](overview/architecture.md)和[设计决策](background/design-decisions.md)。

## 机会地图

```mermaid
flowchart TD
    E[证据链] --> SR[统一安全报告]
    M[可维护性] --> HF[拆分 Herdr 生命周期测试]
    M --> SC[减少 schema 双写]
    H[条件性硬化] --> FR[Receipt 大文件与竞态]
    H --> PX[Principal proxy 分类]
    H --> SSE[Dashboard 连接预算]
    L[生命周期] --> RT[Runtime retention]
```

这些节点不是优先级排序。是否实施取决于触发条件；当前 local-first、单 OS 用户和零运行时依赖的设计本身也是需要保留的价值。

## 1. 统一安全报告的机器真源

**事实依据**

- `justfile` 的 `security` recipe 生成 run-scoped Bandit 与 pip-audit artifacts，secret scan 通过进程退出状态报告。
- `.github/workflows/ci.yml` 根据 `just security` 的 outcome 阻断 gate，并在 `main` 失败时维护一个 insight issue。
- `security-findings.json` 是一次独立的 14 文件、零 finding 快照；`.factory/security-config.json` 另行保存 STRIDE pattern 与 severity policy。
- 当前树中没有 `scripts/check_security_report.py`，也没有现存代码证明这三类机器结果会合并或验证 freshness。

**风险**

维护者可能把 `security-findings.json` 的“零 finding”误当作当前 CI 结果，或在 threat-model version、severity policy、scanner output 之间发生无提示漂移。公开 insight 只指向 Actions run，无法在本地用一个稳定 schema 重放相同判定。

**取舍**

统一报告需要定义 Bandit、pip-audit、secret scan 与 STRIDE 的归一化 schema、去重和 freshness 规则；若只是复制原始结果，会增加又一个可漂移 artifact。报告还必须避免写入 prompt、terminal output、credential 和 exploit details。

**何时处理**

在把 `security-findings.json` 用作 release/merge 证据、增加新 scanner、修改 `.factory/security-config.json` 门槛，或需要跨 CI run 比较 finding 前处理。此前应继续把 `just security` 和 CI outcome 视为实际 gate，把静态 JSON 视为时间点证据。

相关：[安全报告流程](security.md#扫描报告与响应流程) · [可观测性与 Attention](features/observability-and-attention.md)

## 2. 在行数门槛触发前拆分 Herdr 生命周期测试

**事实依据**

- `scripts/check_repository.py` 对 Python source 上限是 1,500 行，测试 Python 是 2,500 行，其他文本是 2,000 行。
- `delivery_recovery.py` 的 helper 已抽到 `delivery_support.py`。`tests/test_skill_package.py` 要求每个受检文本文件至少留 1 行余量。
- `tests/test_herdr.py` 仍同时承载 startup、readiness、prompt reconciliation、settlement、runtime error、blocked response、receipt 与 cleanup ownership，接近测试上限。

**风险**

下一次增加 lifecycle 或 receipt 分支就可能碰到硬门槛；更早的风险是修改者难以判断 fake runner 的长响应序列属于哪个 phase，安全拒绝路径容易与一般 happy path 混杂。

**取舍**

按 startup、turn、receipt、resume/cleanup 拆测试会改善导航，但过度抽 helper 可能隐藏完整 Herdr argv 顺序和 timeout 证据。拆 production 模块也可能把 thread-local deadline、创建 ownership 与 settlement 状态分散到更浅的接口。

**何时处理**

在下一次新增 harness startup 分支、receipt kind、runtime error detector 或 blocked/cleanup 行为时进行，而不是仅为降低行数机械搬运。拆分后仍应让 `tests/test_harness_automation.py` 固定最大自动化参数与 Claude execution-root guard。

相关：[Herdr runtime](systems/herdr-runtime.md) · [任务收据与恢复](features/receipts-and-recovery.md) · [运行时经验](background/runtime-lessons.md)

## 3. 减少模型 artifact 的 prompt/schema 双写

**事实依据**

- `src/herdr_orchestrator/planner.py` 同时手写 planner/router prompt 中的 JSON shape 与对应 loader 的 exact-key/长度规则。
- `src/herdr_orchestrator/topology.py` 同样分别维护 prompt schema 和 `load_topology_decision()`。
- `src/herdr_orchestrator/delivery_prompts.py` 描述交付 artifact，`src/herdr_orchestrator/delivery_protocol.py` 再独立实现 exact-key、枚举、DAG、commit 和长度校验。
- 当前测试覆盖 loader 拒绝路径与主要交付流程，没有证据表明现有 shape 已发生漂移。

**风险**

未来只更新 prompt 或只更新 loader 时，模型会被要求写出 coordinator 必然拒绝的文件；在两次 artifact retry 的上限内，这表现为昂贵但不易定位的稳定失败。

**取舍**

从单一声明生成 prompt 与 validator 能减少双写，但也可能模糊当前精确、可测试的错误码，并引入复杂生成框架或新的运行时依赖。项目当前的手写 schema 很直接，不能为了抽象而牺牲 fail-closed 可读性。

**何时处理**

在新增 artifact 版本、同一字段第三次跨 prompt/loader/renderer 修改，或出现真实 schema drift 回归时处理。合适结果可以只是测试共享的 schema fixture，不必立即引入通用 JSON Schema 引擎。

相关：[Harness catalog 与路由](systems/catalog-and-routing.md) · [交付 Artifact](primitives/delivery-artifacts.md) · [数据模型参考](reference/data-models.md)

## 4. 为 file receipt 定义大小预算与更窄的文件读取原语

**已落地的部分**

`src/herdr_orchestrator/completion.py` 用 `O_NOFOLLOW` 打开常规文件，按块计算 SHA-256，并拒绝超过 1 MiB 的 file receipt。稳定错误码是 `task_receipt_too_large`。

**仍成立的残余**

路径检查与打开之间仍不是跨进程原子的。同 OS 用户可以在验证前替换文件。这个预算服务 sentinel receipt，不把 file receipt 变成构建产物通道。

**何时再处理**

file receipt 被用于构建产物、支持远程/低信任 worker，或项目改变“同 OS 用户可信”假设时。

相关：[任务收据与恢复](features/receipts-and-recovery.md) · [安全](security.md#receiptpane-ownership-与路径安全)

## 5. 明确 runtime state 的 retention、权限与压缩边界

**事实依据**

- `src/herdr_orchestrator/store.py` 持久化原始 job prompt，并以 append-only receipts 保留 attempt 历史。
- `src/herdr_orchestrator/observability.py` 持续 append `events.jsonl`、`metrics.jsonl` 和 `alerts.jsonl`；代码不设置最大文件数、时间或大小。
- 标准化交付失败会保留 artifact、branch 和 worktree 以支持恢复；普通 GC 明确不删除 worktree。
- `docs/observability.md` 把本地文件生命周期交给操作者的 filesystem retention policy。

**风险**

长期运行会累积 prompt、路径、receipt、ledger 和 telemetry；共享账号、备份或磁盘压力会放大本地信息披露与可用性风险。默认创建依赖进程 umask，没有独立的权限/加密层。

**取舍**

自动 prune 会损失 crash recovery、审计时间线、失败现场和幂等 artifact；删除 worktree 还可能丢失未集成代码。压缩/轮转增加恢复与 Dashboard 读取复杂度，应用层加密又引入密钥生命周期。

**何时处理**

在引入常驻 supervisor、共享开发机、合规保留要求、明显磁盘增长或备份 runtime state 前处理。应先区分可重建 telemetry、durable queue、失败交付证据和用户 worktree，不能用单一清空命令处理全部数据。

相关：[Durable execution](features/durable-execution.md) · [可观测性与 Attention](features/observability-and-attention.md) · [Placement 与 worktree](primitives/placement-and-worktrees.md)

## 6. 收敛 observability 目录命名的文档漂移

**已落地**

`docs/observability.md`、实现和 Wiki 都使用 `.orchestrator/telemetry/`。`runner.py` 构造 `config.state_db.parent / "telemetry"`。不要再把文档改回 `observability/`。

相关：[可观测性与 Attention](features/observability-and-attention.md) · [配置参考](reference/configuration.md)

## 7. 扩展 principal-proxy 的受保护类别检测策略

**事实依据**

- `src/herdr_orchestrator/delivery.py` 在调用 controller 前，用一个关键词正则检查 API key、credential、password、secret、token、production/prod。
- `src/herdr_orchestrator/delivery_protocol.py` 强制 `secret`/`production` category 只能 escalate。
- 每个 blocked turn 最多 8 轮，ledger 不记录实际回答；`tests/test_delivery.py` 覆盖明确的 production API token 文本。

**风险**

不含现有英文关键词的敏感问题、其他语言、个人数据、付款、权限变更或生产同义词可能绕过第一层词法 guard，转而依赖不可信 controller 正确分类。

**取舍**

扩大关键词会提高误升级率并打断本地可逆工作；通用敏感信息分类器又会引入模型依赖和不确定性。当前窄 guard 与 strict decision schema 是双层防护，不能用更复杂 prompt 替代确定性 escalation。

**何时处理**

在 principal proxy 获得新的 authority category、覆盖更多语言、允许外部副作用，或真实 blocked transcript 暴露漏检类别时处理。每个新增类别都应先有 fail-closed test 和明确用户升级路径。

相关：[标准化交付](systems/standardized-delivery.md) · [安全](security.md#standardized-deliveryprincipal-proxy-与-tracker-限权)

## 8. 为 Dashboard SSE 增加显式连接预算

**已落地的部分**

`/api/events` 同时最多 16 条连接。超出时返回 `503 {"error":"dashboard_sse_limit"}`，已有流继续。`tests/test_dashboard.py` 覆盖预算与 HTTP 拒绝。

**仍成立的残余**

没有按客户端的时长上限，也没有从 ThreadingHTTPServer 换成单线程 async。loopback 上的本机进程仍可以把预算用满。远程访问不能只靠提高这个数字。

**何时再处理**

Dashboard 变成长驻多客户端服务，或任何人提议扩大 bind 范围前。那时需要认证、授权和 CSRF，而不是只改连接上限。

相关：[本地 Dashboard](systems/dashboard.md) · [安全](security.md#dashboardloopbackhostcsp-与白名单)

## 9. 提升 npm installer 多文件协调的崩溃一致性

**已落地的部分**

`bin/installer-journal.mjs` 在首次 owned mutation 前写 journal；install/uninstall 会先 reconcile。`just test-installer-crash-matrix` 和 `docs/installation.md` 覆盖该路径。

**仍成立的残余**

项目文件、manifest 和 Git common-dir exclude 仍不是同一个内核事务。journal 让 partial install 可恢复，但不能假装跨根目录原子可见。

**何时再处理**

托管根继续增长、无人值守批量部署，或出现 journal 也无法分类的 partial-install 事故时。

相关：[安装与分发](systems/installation-and-distribution.md) · [依赖参考](reference/dependencies.md)

## 10. 对齐 npm publish 的实际权限、文档与测试

**已落地**

`publish` job 只有 `contents: read` 与 `id-token: write`。`github-release` 是单独 job，只有 `contents: write`。`docs/installation.md` 与 `tests/test_release.py` 已固定该拆分。不要再把它们合成一个 job。

相关：[安装与分发](systems/installation-and-distribution.md) · [安全](security.md#ci-与-npm-oidc) · [部署与发布](deployment.md)

## 刻意不列为“清理”的设计边界

以下行为看似可以“自动收拾”，但当前证据表明它们是安全或恢复不变量，不应在普通维护中顺手改变：

| 不应顺手做的事 | 原因 |
| --- | --- |
| 自动删除失败/成功 worktree、branch 或 checkout | Worktree 是可恢复任务与未集成代码证据；普通 GC 明确排除它 |
| 让普通 queue 自动回答 blocked agent | 人工 `resume --response-file` 是权限边界；只有显式标准化交付有有界 principal proxy |
| 把 Dashboard 变成 retry/resume/focus 控制面 | 当前无认证模型只适用于 loopback、只读投影 |
| 把 `idle`/`done` 当成任务成功 | Settlement 与 `task_verified` 是两个独立事实 |
| 把标准化交付合并进普通 SQLite job 状态机 | 两条运行面的授权、artifact、退出码和恢复语义刻意分离 |
| 让 installer 接管内容相同的既有 Skill | 内容相同不等于 ownership；现有测试要求复用但不接管 |

创建正式工作项前，应先用对应测试复现风险、确认 owner 与范围，再按仓库债务策略记录；本页本身不替代 issue tracker。
