在 `/Users/oldwinter/Code/grok-army-campaign` 下，把 GitHub 用户 `oldwinter` 的仓库 clone 成 sibling git checkout。

规则：
- 先 `mkdir -p /Users/oldwinter/Code/grok-army-campaign`。
- 用已登录的 `gh`：`gh repo list oldwinter --limit 200 --json name,url`。
- 已存在且 `origin` 指向 `github.com/oldwinter/<name>` 的目录必须复用，禁止删除、禁止 `git clone` 覆盖。
- 优先 clone 这些软件仓库（若尚未存在）：`herdr-orchestrator`、`hctl`、`all-cli`、`skills-desktop`、`open-pstack`、`multica`、`better-harness`、`munder-difflin`、`raycast-hyper-keyboard`、`official-prompt-image-gallery`、`harnessctl`、`oh-my-mermaid`、`hyperframes`、`herdrm`、`open-design`、`skills`。
- 其余仓库能 clone 就 clone；磁盘或权限失败时记录原因，不要编造 checkout。
- 不要 push、merge、改 remote、删仓库。
- 最终把清单写到 `/Users/oldwinter/Code/herdr-orchestrator/.orchestrator/grok-army/clones.txt`：每行 `name\tpath\tremote\treused|cloned|failed\tnote`。至少成功两个 `github.com/oldwinter/...` checkout。

停止条件：清单文件已写入，且该目录下至少有两个有效 oldwinter clone。
