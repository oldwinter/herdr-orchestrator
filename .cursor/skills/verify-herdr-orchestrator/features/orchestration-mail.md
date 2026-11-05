# Durable worker mail and decision gates

操作者（或 controller）用 durable mailbox 给 worker 留状态、提问、等回执，用 decision gate 把需要人拍板的点显式挂起来。消息和 gate 都写进隔离 `state.db`，重启不丢。

## Sub-features

- `send`：写一条 durable 消息（`kind=status`），`--key` 幂等，重复写只回到同一 id
- `check`：按 handle 读未 ack 的消息，`--after` / `--thread` / `--type` 过滤
- `ack`：把一条消息标记已读，再次 `check` 不再返回
- `gate-create` / `gate-list` / `gate-resolve`：把待决策点落到 `job_id` 上，resolve 后保留 `resolution` 与 `resolved_at`
- `task-list`：读回 job 行（与 `status` 同一份 queue，但更完整）

`ask` / `reply` 依赖受监督的真实 attempt（`--job-id` / `--attempt-id` / `--token`），在隔离 queue 里会返回 `supervision_attempt_required`。默认证明**不含** `ask` / `reply`；它们是 `verified-unreachable`，不是失败。

## How to get to it (user POV)

1. controller 在隔离 workflow 上给 worker 发一条状态消息（`send --from … --to … --key …`）。
2. worker 用 `check --handle <自己>` 读，读完 `ack --id <id>`，再 `check` 就空了。
3. 需要人拍板时 `gate-create --job-id <真实 job id> --id <gate id> --question … --option …`。
4. 操作者在 `gate-list` 看到 `status=pending`；拍板后 `gate-resolve --id … --resolution <option>`，`gate-list` 里变成 `status=resolved`。
5. `task-list` 能看到 queue 里 job 行的完整字段（`dedupe_key`、`placement`、`completion_policy`、attempts）。

## Driving it with CLI

`--workflow` 是 **父命令** 的参数，必须写在子命令之前。

```bash
WORKFLOW="$(python3 -c 'import json; print(json.load(open(".orchestrator/verify-scratch/" + open(".orchestrator/verify-scratch/current").read().strip() + "/run.json"))["workflow"])')"
# 1. send + 幂等复写
PYTHONPATH=src python3 -m herdr_orchestrator orchestration --workflow "$WORKFLOW" \
  send --from verify-controller --to verify-worker \
  --body "Verify durable mail" --key verify-mail-v1 --subject "Verify mail"
# 再跑一次同一 --key：仍是同一个 message_ids

# 2. check → ack → check
PYTHONPATH=src python3 -m herdr_orchestrator orchestration --workflow "$WORKFLOW" \
  check --handle verify-worker
PYTHONPATH=src python3 -m herdr_orchestrator orchestration --workflow "$WORKFLOW" \
  ack --handle verify-worker --id 1
PYTHONPATH=src python3 -m herdr_orchestrator orchestration --workflow "$WORKFLOW" \
  check --handle verify-worker

# 3. gate 生命周期（job id 用 status/task-list 里的真实 seed id）
PYTHONPATH=src python3 -m herdr_orchestrator orchestration --workflow "$WORKFLOW" \
  gate-create --job-id 1 --id verify-gate-1 \
  --question "Ship the verify gate?" --option yes --option no
PYTHONPATH=src python3 -m herdr_orchestrator orchestration --workflow "$WORKFLOW" \
  gate-list
PYTHONPATH=src python3 -m herdr_orchestrator orchestration --workflow "$WORKFLOW" \
  gate-resolve --id verify-gate-1 --resolution yes
PYTHONPATH=src python3 -m herdr_orchestrator orchestration --workflow "$WORKFLOW" \
  gate-list

# 4. queue 全字段
PYTHONPATH=src python3 -m herdr_orchestrator orchestration --workflow "$WORKFLOW" \
  task-list
```

证明结束态：

- `send` 输出 `message_ids` 非空，`completion_authority` 为 `false`；重复 `--key` 返回同一 id，不新增行。
- `check --handle verify-worker` 的 `messages[0]` 有 `body` / `sender` / `recipient` / `dedupe_key` / `thread_id`，`acknowledged_at` 为 `null`。
- `ack` 输出 `{"acknowledged": 1}`；随后 `check` 的 `messages` 为空、`timed_out` 为 `false`。
- `gate-create` 输出 `gate_id`；`gate-list` 里该 gate `status=pending`、`options` 是 JSON 字符串。
- `gate-resolve` 输出 `status=resolved`；`gate-list` 里 `resolution=yes`、`resolved_at` 非空。
- `task-list` 的 `jobs[]` 含两张 seed，字段与 `status` 一致。
- 证据写进 `.orchestrator/verify-evidence/<run-id>/`：`mail-send.json` `mail-send-dup.json` `mail-check.json` `mail-ack.json` `mail-check-after-ack.json` `gate-create.json` `gate-list.json` `gate-resolve.json` `gate-list-after.json` `task-list.json`。

## Gotchas

- `--workflow` 在子命令**之前**；写成 `orchestration send --workflow …` 会被父 parser 拒绝（`error: the following arguments are required: --workflow`）。
- `ask` / `reply` 需要真实 job attempt 的 `--job-id/--attempt-id/--token`，隔离 queue 里 `ask` 直接退 `supervision_attempt_required`。不要为了让它变绿去伪造 attempt；把这一条记成 unreachable。
- `check` 默认只返回未 ack 的消息；要读历史用 `--all`。
- `gate-list` 的 `options` 是 JSON **字符串**，不是数组。
- 消息和 gate 都以 workflow 的 `state.db` 为准；不要复制 db 去“隔离读”，页面/CLI 会读到不同的文件。
- `send` 的 `--body` 是纯文本参数，不要塞多行 shell 字符串；需要长文时另开消息或写 `--subject`。
