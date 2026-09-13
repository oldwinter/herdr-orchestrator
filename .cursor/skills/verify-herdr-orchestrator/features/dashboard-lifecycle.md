# Dashboard recent lifecycle

操作者在页面底部 Recent lifecycle 阅读 durable 入队与收据。最多 100 条证据，页面渲染最近 24 条。刷新浏览器或重启 Dashboard 不应丢掉这些事件。

## Sub-features

- 任务创建事件 `type=enqueued`，id 形如 `job:<id>:created`
- receipt 事件（本默认环没有 `run`，通常没有）
- 阅读锚点 / FLIP（需后续 snapshot，不是冷启动必测）

默认证明：seed 之后时间线至少两条 `enqueued`，标题匹配 seed。

## How to get to it (user POV)

1. 同一 Dashboard URL，滚到 `h2` 为 `Recent lifecycle` 的 panel。
2. 每条 `.timeline-event[data-event-id]` 显示 title、`enqueued · pending`、以及 `droid · …` / `codex · …` detail。
3. 空态文案是 `No lifecycle events yet` / `Loading lifecycle…`；seed 成功后不应停留在这两种。

## Driving it with browser + HTTP

```bash
# doctor 已把 snapshot 写到 evidence
python3 - <<'PY'
import json, pathlib
run = pathlib.Path(".orchestrator/verify-scratch/current").read_text().strip()
snap = json.loads(pathlib.Path(f".orchestrator/verify-evidence/{run}/snapshot.json").read_text())
events = snap["snapshot"]["timeline"]
titles = {event["title"] for event in events if event.get("type") == "enqueued"}
assert "Verify droid inventory" in titles
assert "Verify codex architecture" in titles
print(json.dumps({"enqueued": len(titles), "shown": len(events)}, indent=2))
PY
```

浏览器：

1. 打开隔离 `url`，等看板出现 job 卡。
2. 断言 `#timeline` 里至少两个 `[data-event-id^="job:"]`（dump-dom 里是 `data-event-id="job:1:created"` 这种）。
3. 截图需包含时间线，不只看板。可再跑一次 `capture-dashboard.sh`（窗口高度 1400 已覆盖该 panel）。

证明结束态：snapshot `timeline` 含两条 seed 的 `enqueued`；DOM 含对应 title；重启隔离 dashboard（先记下 evidence，cleanup+launch 会是新 db——要测持久性应只杀 pid 再以**同一** `workflow.toml`/`state.db` 拉起，不要删 scratch）。默认环不要求重启持久性。

## Gotchas

- 时间线来自 jobs.created_at 与 receipts，不是 Herdr pane output。无 Herdr 也能绿。
- 页面只渲染 24 条，API 最多 100。两条 seed 不会碰到截断。
- `data-event-id` 对 created 事件是 `job:<id>:created`，对收据是 `receipt:<id>`。
- 不要把 `just run` 制造 receipt 当成这个 feature 的前置。
