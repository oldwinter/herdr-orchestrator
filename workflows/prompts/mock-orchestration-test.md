这是编排系统的一次 mock 连通性测试。只读取指定文件，不要执行外部动作。除步骤 3
明确的 receipt 文件外，不要写入或修改任何文件。

任务：
1. 读取 workflows/multi-harness.toml，确认 `[coordinator]` 段的 poll_seconds、max_parallel、lease_seconds、max_attempts 值。
2. 读取 docs/architecture.md 的标题，确认第一行标题文本。
3. 在当前执行根目录下创建一个 JSON 文件 `.orchestrator/probes/mock-orchestration-<YYYYMMDD>.json`，内容为：
   ```json
   {"mock": true, "harness": "<你的 harness 名>", "coordinator_ok": true}
   ```
   把 `<YYYYMMDD>` 替换为当天日期（如 20260825），把 `<你的 harness 名>` 替换为你实际运行的 harness。
4. 用一行 JSON 输出你的结论：`{"harness": "...", "seen_poll_seconds": N, "architecture_title": "..."}`。

注意：步骤 3 创建的文件是唯一允许的写入，它会作为本次任务的 receipt 被验证。
