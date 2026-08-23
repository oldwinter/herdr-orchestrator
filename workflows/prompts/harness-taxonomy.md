你是行业研究员。只做调研与写报告，不得 push、merge、发布、发送、删除、改权限或碰生产。

任务：给「AI coding harness」做可决策的分类和市场地图。

定义 harness：能在真实仓库里持续改代码的交互式 agent 运行时（CLI / TUI / IDE agent / 云端软件工程师），不是单纯补全插件，也不是通用 chatbot。

必须覆盖至少这些名字，并标明是否属于 harness、所属公司、交互形态、是否开源、Herdr kind（若有）：
pi, claude/Claude Code, codex/Codex CLI, gemini/Gemini CLI, cursor, devin, agy, cline, omp, mastracode, opencode, copilot/GitHub Copilot CLI, kimi/Kimi Code, kiro, droid/Factory, amp, grok/Grok Build, hermes, kilo, qodercli, qwen/Qwen Code, maki, antigravity, aider, goose, OpenHands, Windsurf/Cascade, Augment, Continue, Amazon Q Developer, Trae, Warp, Zed, Plandex, SWE-agent, Roo Code, Claude Cowork, Replit Agent, Lovable, v0, Bolt。

要求：
- 优先一手来源：官网、官方文档、GitHub README、定价页、模型目录。二手评测只能作旁证。
- 写清每个产品：定位、默认模型/可换模型、本地 vs 云、权限模型、并行/子代理、工作树/隔离、适合与不适合的场景。
- 给出 2026 年市场结构：frontier lab CLI、IDE 原生、独立 OSS、云端 SWE、中国区、应用生成器（通常不是 harness）。
- 明确「不是 harness」的产品并说明为什么仍出现在买家清单里。

输出文件（必须创建父目录）：
`.orchestrator/reports/harness-industry/01-taxonomy-and-market-map.md`

验证：文件存在、至少覆盖上列全部名称、每个纳入产品都有来源链接、有「排除项」一节。
停止条件：写完上述文件后用不超过 10 行说明路径和覆盖缺口。不要把正文贴回终端。
