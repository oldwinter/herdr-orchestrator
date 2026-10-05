# Dashboard work-in-flight board

操作者打开本地 Operations console，看摘要数字和 Queued / In motion / Attention / Finished 四列。Dashboard 只读，不能从页面 retry 或回答 blocked。

## Sub-features

- 顶部 Workflow summary 六格指标
- Work in flight kanban 与 `article.job-card[data-job-id]`
- 连接状态 `#connection-label`（`Live` / `Connected` / `Reconnecting`）
- 源健康警告 `#source-warning`（queue / Herdr / SSE）
- 窄屏列导航（≤760px 的 `#kanban-navigation`）

默认证明：**1440px 桌面宽度下的摘要 + Queued 列**。窄屏导航是加分项。

## How to get to it (user POV)

1. 文档入口是 `just dashboard`，默认 `http://127.0.0.1:8765`。
2. 验证入口是 `helpers/launch.sh` 打印的 `url`（端口由 OS 分配）。
3. 浏览器打开该 URL。eyebrow 为 `Local operations console`，`#workflow-name` 为 `verify-orchestrator`。
4. 等第一帧 snapshot：Pending 为 2（或更多），Queued 列出现两张 seed 卡，In motion / Attention / Finished 为空态（`No in motion jobs` 等）。

## Driving it with browser + HTTP

先 doctor，再走真实页面：

```bash
.cursor/skills/verify-herdr-orchestrator/helpers/drive-dashboard-board.sh
```

人工/浏览器步骤：

1. 打开 `run.json` 的 `url`。
2. 等 `#metric-pending` 不是 `—`，`#connection-label` 为 `Live` 或 `Connected`。
3. 在 `#kanban-column-queued`（`data-column-state="populated"`）读取两张 `.job-card`：`h3` 分别为 `Verify droid inventory` 与 `Verify codex architecture`；`.state-badge` 为 `pending`；worker `dd` 为 `droid` / `codex`。
4. `#job-total` 为 `2 jobs`（若还没 enqueue 第三张）。
5. 不要点拓扑缩放当主证明；看板不依赖 Canvas。

机器核对（与页面同一 snapshot）：

- `GET /api/snapshot` → `summary.pending >= 2`，jobs 标题集合包含两张 seed。
- dump-dom 含 `data-job-id=`、两个 title、`Herdr Operations`。
- `board.png` 能读出 Queued 列标题，不是 “Loading queue state…”。

## Gotchas

- `/` 的静态 HTML 在 JS 跑完前是 “Loading queue state…”。curl 首页不能当证明；要用 Chrome dump-dom 或等 `#kanban .job-card`。
- 页面会同时 `fetch /api/snapshot` 和 `EventSource /api/events`。health 未 ready 时 initial fetch 会失败并显示 `Queue state unavailable`。
- Host 必须是 `127.0.0.1` 或 `localhost`，且带对端口；否则 421。
- 无 Herdr 时警告条出现是预期。不要为了截图好看去关警告。
- 不要对用户的 8765 做这个 drive。`#workflow-name` 若是 `multi-harness` 就立刻停。
- Dashboard 打不开不存在的 `state_db`，也不会迁移旧 schema。必须先 seed。
