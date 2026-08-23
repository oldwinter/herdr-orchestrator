你是决策分析师。只做调研与写报告，不得 push、merge、发布、发送、删除、改权限或碰生产。

任务：产出「场景 × harness × 模型」的最优解矩阵，供个人和团队选型。不要写空泛趋势。

先读（若已存在）：
- `.orchestrator/reports/harness-industry/01-taxonomy-and-market-map.md`
- `.orchestrator/reports/harness-industry/02-frontier-lab-clis.md`
- `.orchestrator/reports/harness-industry/03-ide-oss-enterprise.md`
若文件不存在，就按你能独立核实的一手来源写，并标注哪些判断缺少前序章节。

必须覆盖的场景：
1. 个人本地仓库，日常实现与重构
2. 高风险变更（auth、支付、数据删除、生产配置）
3. 大规模只读审计 / 架构梳理
4. 多 agent 并行（实现 + 评审 + 调研）
5. 长时间 detach / 隔夜任务
6. CI 或无人值守
7. 企业合规（代码不出域、SSO、审计）
8. 低成本 / 本地开源模型
9. 中国区网络与模型可达
10. 本仓库这种「多 harness 编排控制面」

对每个场景给出：
- 首选 harness + 首选模型
- 可接受替补（最多 2 个）
- 明确不要用的组合
- 理由（工具服从、上下文、权限、价格、恢复、生态）
- 验证方式

另给一张总表：行是场景，列是推荐。最后用 20 条以内的决策规则收口，规则必须可执行。

输出文件（必须创建父目录）：
`.orchestrator/reports/harness-industry/04-scenario-model-matrix.md`

验证：10 个场景都有首选和禁止项；总表完整；决策规则可独立执行。
停止条件：写完文件后用不超过 10 行说明路径。不要把正文贴回终端。
