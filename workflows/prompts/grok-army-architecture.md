在 `/Users/oldwinter/Code/grok-army-campaign` 里选一个已 clone 的 oldwinter 软件仓库（优先 `herdr-orchestrator`、`hctl`、`open-pstack`、`all-cli`），做一轮「架构」工作。

步骤：
1. 阅读该仓库的 README、架构文档或模块入口，找出一个具体、可本地修复的架构问题（分层、职责边界、重复模块、配置真源、恢复语义）。
2. 用已登录 `gh` 在该 GitHub 仓库创建一个 issue。标题或正文必须包含「架构」。正文写清现状、为何是问题、建议改动、不在范围内的事。不要空 issue，不要重复已有同题 issue。
3. 在该 clone 内做对应的本地改进（文档或小范围代码），不要 push、不要开 PR、不要 merge。
4. 把 issue URL、仓库路径、改动文件列表追加写入 `/Users/oldwinter/Code/herdr-orchestrator/.orchestrator/grok-army/issues.txt`。

停止条件：issue URL 可打开，本地至少有一个与该架构问题对应的文件改动。
