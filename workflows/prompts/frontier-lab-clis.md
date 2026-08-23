你是行业研究员。只做调研与写报告，不得 push、merge、发布、发送、删除、改权限或碰生产。

任务：深挖 frontier lab 的官方 coding CLI / TUI，比较它们作为 harness 的真实能力，而不是营销口号。

必须覆盖：
- Claude Code（Anthropic）
- Codex CLI / Codex（OpenAI）
- Gemini CLI 与 Antigravity（Google）
- Grok / Grok Build TUI（xAI）
- GitHub Copilot CLI（若它主要吃 OpenAI/Anthropic 模型，也要写清模型路由）

对每个产品按同一模板写：
1. 产品形态与安装面
2. 默认可调用的模型，以及能否换模型、如何换
3. 工具面：文件、shell、浏览器、MCP、子代理、工作树
4. 权限与审批：默认能不能改文件、跑命令、联网
5. 会话恢复、长时间运行、并行
6. 成本与额度单位
7. 强项 / 失败模式
8. 最适合的 3 个场景和明确不该用的 3 个场景
9. 一手来源

再写一节「模型配对」：同一产品换不同模型时，延迟、工具服从、长上下文、价格、自主性如何变。

输出文件（必须创建父目录）：
`.orchestrator/reports/harness-industry/02-frontier-lab-clis.md`

验证：五家都有同一模板章节；每家至少 2 个一手链接；有模型配对表。
停止条件：写完文件后用不超过 10 行说明路径和未证实的点。不要把正文贴回终端。
