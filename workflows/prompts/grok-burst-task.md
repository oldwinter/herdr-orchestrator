在 `/Users/oldwinter/Code/grok-army-campaign` 里对你任务标题中点名的那个仓库做一轮有用的工程改进。

步骤：
1. 确认该目录已是 git clone，`origin` 指向 `github.com/oldwinter/<name>`。不存在就停并说明，不要重新 clone 覆盖。
2. 读 README 与入口代码，找一个具体、可本地修复的「架构 / 体验 / 设计」问题。不要空话。
3. 用已登录 `gh issue create --repo oldwinter/<name>` 开一个真实 issue。标题或正文必须包含「架构」「体验」或「设计」之一。不要重复已有同题。
4. 在该 clone 内做对应本地改进。不要 push、不要开 PR、不要 merge。
5. 把 issue URL、仓库路径、改动文件追加到 `/Users/oldwinter/Code/herdr-orchestrator/.orchestrator/grok-burst/issues.txt`。

停止条件：issue URL 可打开，且本地至少有一个对应文件改动。远早于 10800 秒结束。
