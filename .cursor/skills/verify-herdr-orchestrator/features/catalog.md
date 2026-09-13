# Compact harness catalog

操作者在派发前查看当前 workflow 启用的 L0 catalog：名字、摘要、适用/避开、traits。完整 `.md` profile 不在这一步加载。

## Sub-features

- 文本目录（`just catalog` 在默认 workflow 上的格式）
- JSON 目录（CLI 默认 `--format json`）
- 按 harness 拉完整 profile（`profile <harness>`，只在选中后才读）

本 feature 默认只证明 **当前隔离 workflow 的 compact JSON catalog**。`profile` 会读完整 Markdown，可作加分项，不是看板证明的前置。

## How to get to it (user POV)

1. 在仓库根，对**隔离** workflow 问 catalog，而不是 `just catalog`（那会打到 `multi-harness`）。
2. 操作者阅读 6 行 harness：`droid` `grok` `codex` `pi` `claude` `hermes`。
3. 需要执行细节时再 `profile droid`，看到 `# Dynamically loaded harness profile` 之前的完整上下文。

## Driving it with CLI

```bash
WORKFLOW="$(python3 -c 'import json; print(json.load(open(".orchestrator/verify-scratch/" + open(".orchestrator/verify-scratch/current").read().strip() + "/run.json"))["workflow"])')"
PYTHONPATH=src python3 -m herdr_orchestrator catalog --workflow "$WORKFLOW" --format json
```

证明结束态：

- 退出码 0。
- `schema_version == 1`。
- `harnesses` 长度 6，`harness` 集合恰好是 `claude` `codex` `droid` `grok` `hermes` `pi`。
- 每个条目有非空 `display_name` `summary` `strengths` `best_for` `avoid_for` `traits`，以及 `profile_ref` 形如 `harness:droid`。
- 把 JSON 存到 `.orchestrator/verify-evidence/<run-id>/catalog.json`。

文本格式对照（可选）：`--format text` 第一行是 `Available harnesses:`，随后 `- droid (Factory Droid)`。

加分：`profile --workflow "$WORKFLOW" droid --format json` 的 `profile.context` 非空且来自 `profiles/harnesses/droid.md`。不要一次读完全部 6 个 `.md`。

## Gotchas

- `just catalog` 绑定 `workflows/multi-harness.toml`。验证必须带 `--workflow` 指向 scratch TOML。
- catalog 只反映 **该 workflow 声明的 workers**。改 template 少一个 worker，JSON 就会少一项——那时应改 map，而不是改断言去迁就。
- planner 在隔离 workflow 里是关闭的；catalog 仍列出 worker harness，不会因为 planner=auto 多出第七项。
