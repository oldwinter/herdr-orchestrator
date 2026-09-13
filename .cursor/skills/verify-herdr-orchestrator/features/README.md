# Feature map

herdr-orchestrator 的操作者表面。验证时用隔离 workflow `verify-orchestrator`，不要用 `workflows/multi-harness.toml`。

| Feature | 表面 | 文件 | 默认证明 |
| --- | --- | --- | --- |
| Compact harness catalog | CLI | [catalog.md](catalog.md) | `catalog --format json` 列出 6 个启用 harness |
| Durable queue seed + status | CLI | [durable-queue.md](durable-queue.md) | `seed` 后 `status` 看到两张 pending |
| Dashboard work-in-flight board | Web | [dashboard-queue-board.md](dashboard-queue-board.md) | 启动后跑 `helpers/drive-dashboard-board.sh` |
| Dashboard recent lifecycle | Web | [dashboard-lifecycle.md](dashboard-lifecycle.md) | `#timeline` 出现 `enqueued` 事件 |
| Enqueue then project | CLI + Web | [enqueue-and-project.md](enqueue-and-project.md) | 显式 `enqueue` 后第三张卡出现在 Queued |

未入图（缺 Herdr 或会碰到真实 agent，先不要当默认环）：

- Herdr topology canvas（无 runtime 时 `#topology-empty` 为 `Waiting for Herdr…` 或 observation unavailable）
- `run` / `retry` / `resume` / `gc --apply`
- `just doctor` / smoke / readiness-matrix
- Manual manager
- Standardized delivery
