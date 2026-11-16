# Enqueue then project

操作者在 CLI 再丢一条任务，Dashboard 在下一次 snapshot（默认 1s poll，SSE 推送）把新卡放到 Queued。用来证明控制面写入与只读投影是同一 queue。

## Sub-features

- 显式 `--harness` enqueue
- `dedupe_key` 冲突（第二次 `created: false`）
- 看板出现第三张 `article.job-card`
- 时间线多一条 `enqueued`

不要测 `enqueue` 省略 harness（auto router）或 `enqueue-auto`。

前置：controller 与至少一个 worker harness 必须在 Herdr session 里可用（`HERDR_ENV=1`，`herdr` 能看到该 pane）。没有时 enqueue 退 `worker_harness_unavailable:<harness>:not_in_herdr`；显式 `--harness` 不绕过这条检查。

## How to get to it (user POV)

1. 隔离 Dashboard 已在跑，Queued 里已有两张 seed。
2. 在另一个终端对**同一** scratch workflow enqueue。
3. 回到浏览器：`#metric-pending` 变成 3，`#job-total` 为 `3 jobs`，Queued 出现 `Verify extra enqueue`。

## Driving it with CLI + browser

```bash
WORKFLOW="$(python3 -c 'import json; print(json.load(open(".orchestrator/verify-scratch/" + open(".orchestrator/verify-scratch/current").read().strip() + "/run.json"))["workflow"])')"
PYTHONPATH=src python3 -m herdr_orchestrator enqueue --workflow "$WORKFLOW" \
  --harness droid \
  --title "Verify extra enqueue" \
  --prompt-file workflows/prompts/droid-inventory.md \
  --dedupe-key "verify-extra-enqueue-v1"
```

期望 stdout：`{"created": true, "harness": "droid", "job_id": <int>}`。把该行写入 evidence `enqueue.json`。

本机不在 Herdr session 时会在这一步退 `worker_harness_unavailable:droid:not_in_herdr`（exit 2）。那是环境前置缺失，记 `verified-unreachable` 并保留该行输出，不要伪造 `enqueue.json`，也不要绑用户的 Herdr session。

然后：

1. `GET /api/snapshot` 直到 jobs 标题包含 `Verify extra enqueue`（最多等 3s）。
2. `helpers/capture-dashboard.sh`。
3. dump-dom 与截图都能看到第三张卡；`#kanban-column-queued .column-count` 为 3。

重复同一 `dedupe-key`：`created` 为 false，job_id 不变，看板仍是 3 张。

证明结束态：status `counts.pending == 3`；snapshot 与 DOM 同时有三张标题；默认 `state.db` 没有 `verify-extra-enqueue-v1`。

## Gotchas

- `--prompt-file` 相对仓库根；CLI 会 resolve 成绝对路径。文件必须存在。
- `dedupe_key` 字符集是 `[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}`。
- Dashboard 只读打开 db。enqueue 写同一文件是 SQLite 常规用法；不要为了“隔离读”去复制 db，否则页面不会更新。
- 不要 `run-once` 来证明 enqueue。pending 出现即成功。
- 隔离 queue 的 `status.preflight` 会把所有 harness 记为 `not_in_herdr`，而 Dashboard snapshot 的 `source_health.herdr` 可能仍是 `ok`。前者只说明 CLI 进程没有 Herdr session，不是 queue 损坏。
- `--placement auto` 即可。显式 `worktree` 在非 Git 或无 Herdr 时不是这条 feature 的范围。
