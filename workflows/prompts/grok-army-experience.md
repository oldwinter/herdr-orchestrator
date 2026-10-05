在 `/Users/oldwinter/Code/grok-army-campaign` 里选一个已 clone 的 oldwinter 软件仓库（优先 `skills-desktop`、`hctl`、`all-cli`、`herdrm`），做一轮「体验」工作。

步骤：
1. 阅读该仓库的 CLI/UI/README 入口，找出一个具体的使用体验问题（帮助文本、错误信息、默认路径、命令发现、空状态）。
2. 用已登录 `gh` 在该 GitHub 仓库创建一个 issue。标题或正文必须包含「体验」。正文写清用户路径、摩擦点、建议改动。不要空 issue，不要重复已有同题 issue。
3. 在该 clone 内做对应的本地改进，不要 push、不要开 PR、不要 merge。
4. 把 issue URL、仓库路径、改动文件列表追加写入 `/Users/oldwinter/Code/herdr-orchestrator/.orchestrator/grok-army/issues.txt`。

停止条件：issue URL 可打开，本地至少有一个与该体验问题对应的文件改动。
