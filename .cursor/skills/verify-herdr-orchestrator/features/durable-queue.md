# Durable queue seed + status

操作者把示例任务幂等写入 SQLite queue，并用 `status` 看 pending 计数和任务行。这是 Dashboard 看板的数据来源。

## Sub-features

- `seed`：按 workflow `[[seed_jobs]]` 插入，重复跑只增加 `existing`
- `status`：`counts`、`jobs`、`harness_health`、`workflow`
- `dedupe_key`：相同 key 不重复入队

默认证明 `launch.sh` 已经做过的那次 seed + 随后 `status`。不要对默认 `state.db` seed。

## How to get to it (user POV)

1. 日常文档写的是 `just seed` 然后 `just status`。
2. 验证时这两条命令会污染操作者 queue，所以改走隔离 helper：`helpers/launch.sh` 内的 `seed`，再 `doctor.sh`（它会跑隔离 `status`）。
3. 操作者在 JSON 里看到两张卡：`Verify droid inventory`（droid）和 `Verify codex architecture`（codex），`state=pending`。

## Driving it with CLI

`launch.sh` 已经 seed。复验：

```bash
WORKFLOW="$(python3 -c 'import json; print(json.load(open(".orchestrator/verify-scratch/" + open(".orchestrator/verify-scratch/current").read().strip() + "/run.json"))["workflow"])')"
PYTHONPATH=src python3 -m herdr_orchestrator seed --workflow "$WORKFLOW"
PYTHONPATH=src python3 -m herdr_orchestrator status --workflow "$WORKFLOW"
```

证明结束态：

- 第二次 `seed` 输出 `{"added": 0, "existing": 2}`。
- `status.workflow == "verify-orchestrator"`。
- `status.counts.pending >= 2`，`running`/`succeeded`/`blocked`/`failed` 为 0（刚 seed、没 `run`）。
- `jobs` 里能看到上述两个 title、对应 harness、`dedupe_key` 为 `verify-droid-inventory-v1` / `verify-codex-architecture-v1`。
- 默认 `.orchestrator/state.db` 若不存在就算隔离成功；若存在，不得出现这两个 dedupe_key。
- 把 `status.json` 留在 evidence 目录（`doctor.sh` 已写）。

## Gotchas

- `seed` 会 `Store.initialize()`。对默认 workflow 跑一次就会创建/迁移用户数据库。
- `status` 附带 `harness_health`。没有 CLI 的 harness 会 `unavailable`；这不影响 pending 入队。
- 不要用 `run-once` 把 pending 变成 running 来“看起来更真”——那会启动 Herdr agent。
- prompt 文件来自 `workflows/prompts/droid-inventory.md` 与 `codex-architecture.md`。seed 只把路径写入 job，不执行 prompt。
