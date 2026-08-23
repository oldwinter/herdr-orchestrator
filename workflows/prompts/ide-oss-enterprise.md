你是行业研究员。只做调研与写报告，不得 push、merge、发布、发送、删除、改权限或碰生产。

任务：深挖非 frontier-lab-CLI 的主流 harness，分成三组写，组内用同一比较轴。

A. IDE 原生 / 编辑器 agent
Cursor Agent/CLI、Windsurf Cascade、Zed、Warp、Continue、Amazon Q、Kiro、Trae。

B. 独立 OSS / 社区运行时
Aider、Cline、Roo Code、Kilo、OpenCode、Goose、OpenHands、Plandex、SWE-agent、pi、hermes、qwen code、kimi code、qodercli。

C. 云端 / 企业软件工程师
Devin、Factory Droid、Amp、Augment、Copilot coding agent（云端）、Replit Agent。
应用生成器 Lovable / v0 / Bolt 只写「为何通常不是仓库 harness」。

比较轴（每个产品都要填，未知就写 unknown 并说明缺什么证据）：
- 交互面（IDE / CLI / 云）
- 默认可换模型
- 仓库写入与 git 工作流
- 并行、子代理、工作树
- 本地代码是否上传
- 企业 SSO / SOC2 / 审计
- 价格单位
- 最强场景 / 最弱场景

输出文件（必须创建父目录）：
`.orchestrator/reports/harness-industry/03-ide-oss-enterprise.md`

验证：三组都有产品表；每个纳入产品都有来源；unknown 不得假装成已知。
停止条件：写完文件后用不超过 10 行说明路径和证据缺口。不要把正文贴回终端。
