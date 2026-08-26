# CLI 参考
Active contributors: oldwinter, chendongdong

参数真源是 `src/herdr_orchestrator/cli.py`，源码 checkout 的稳定 recipe 真源是
`justfile`。本页聚焦 runtime；更严格的 automation/wire 约定见
[API / CLI contracts](../api/cli-contracts.md)。

## 入口

```bash
# 源码 checkout：直接调用 Python runtime
PYTHONPATH=src python3 -m herdr_orchestrator status \
  --workflow workflows/multi-harness.toml

# 源码 checkout：稳定 recipe
just status

# npm 安装到目标项目后：wrapper 自动注入已安装 workflow
npx --yes herdr-orchestrator status --project .
```

直接 Python CLI 的每个子命令都要求 `--workflow <path>`。`justfile` 默认把它设为
`workflows/multi-harness.toml`；npm wrapper 则使用目标项目下由 manifest 管理的
`.herdr-orchestrator/workflows/multi-harness.toml`。

相对的 workflow、prompt、response 与 goal CLI 路径由进程当前目录解析。错误信息写
stderr；除 text 格式的 catalog/profile 和长运行的停止提示外，命令结果使用 JSON。

## 稳定 runtime 命令面

| Python 子命令 | `just` 入口 | 用途 |
| --- | --- | --- |
| `doctor` | `just doctor` | 系统、Herdr、profile 与真实 harness readiness |
| `catalog` | `just catalog`, `just catalog-json` | 当前 worker pool 的 compact catalog |
| `profile` | `just profile <harness>` | 按需读取一个完整 harness context |
| `seed` | `just seed` | 幂等写入 `[[seed_jobs]]` |
| `enqueue` | `just enqueue ...`, `just enqueue-auto ...` | 写入单个 durable job |
| `run` | `just run`, `just run-once`, `just run-until-idle` | 持续、单 wave 或有界排空 |
| `status` | `just status` | Queue counts 与 job 当前视图 |
| `retry` | `just retry <job-id>` | 只对 failed job 增加 attempt budget |
| `resume` | `just resume <job-id> <response-file>` | 人工恢复 blocked agent/pane/attempt |
| `gc` | `just gc`, `just gc-failed` | 预览或关闭本 workflow 拥有的终态 agent pane |
| `dashboard` | `just dashboard` | 启动本机只读实时 Dashboard |
| `smoke` | `just smoke` | 对启用 harness 做真实只读 turn |
| `deliver` | `just deliver <goal-file>` | 显式 opt-in 标准化交付 |

`just test`、`just test-coverage`、`just lint`、`just security`、`just docs-check` 与
`just check` 是开发/质量命令，不是 coordinator runtime 子命令。

## 通用选择参数

`run`、`enqueue`、`deliver` 接受：

| 参数 | 取值与语义 |
| --- | --- |
| `--controller-harness` | `auto` 或六个 harness；省略遵循 `[planner].harness`，显式 `auto` 强制本机确定性选择 |
| `--worker-harness` | 六个 harness 之一；可重复，整个列表替代 TOML worker candidate pool |

有效 worker override 必须无重复，且每项都对应 workflow 中已声明的 worker。

## 命令与参数

### `doctor`

```text
doctor --workflow PATH
       [--probe-timeout-seconds N]
       [--harness droid|grok|codex|pi|claude|hermes ...]
```

- Probe timeout 默认 30 秒，运行时范围 5–300。
- `--harness` 可重复，只探测 workflow 已启用的 harness；选择未启用值是命令错误。
- 输出 `{checks, ok, summary}`。每个 readiness check 可含 `status`、`error_code`、
  `error_summary`、`duration_ms` 与 `phase_timings_ms`。
- 所有 check 通过退出 0；任何 readiness/system check 失败退出 1。

### `catalog` 与 `profile`

```text
catalog --workflow PATH [--format json|text]
profile --workflow PATH HARNESS [--format json|text]
```

- `catalog` 默认 JSON，结构为 `{"schema_version":1,"harnesses":[...]}`。
- `catalog --format text` 输出可读摘要。
- `profile` 的 `HARNESS` 是位置参数；默认 text，只输出 context Markdown。
- `profile --format json` 输出 `{"schema_version":1,"profile":{compact fields...,"context":"..."}}`。

### `seed`

```text
seed --workflow PATH
```

输出：

```json
{"added": 2, "existing": 4}
```

数字分别表示本次新建和由 workflow-scoped `dedupe_key` 命中的 seed 数。

### `enqueue`

```text
enqueue --workflow PATH
        [--harness auto|droid|grok|codex|pi|claude|hermes]
        --title TEXT
        --prompt-file PATH
        --dedupe-key KEY
        [--placement auto|tab|pane|worktree]
        [--receipt-prefix TEXT | --receipt-file RELATIVE_PATH]
        [controller/worker selection options]
```

- `--harness` 默认 `auto`；显式 harness 跳过 worker router，但仍必须位于有效 pool。
- `--placement` 默认 `auto`，表示遵循 worker/global/hybrid policy。
- Prompt 文件必须存在且去空白后非空。
- Dedupe 命中时不新建，也不会因本次参数不同而改写已有 job；输出已有 job id 与原 harness。
- `--receipt-prefix` 与 `--receipt-file` 互斥：
  - prefix 去空白后必须非空、最多 256 字符且不能含换行；只有 dispatch 后新增 output 中某行以该值开头才算成功，prompt 自身含相同 receipt line 会被拒绝为歧义；
  - file 值最多 500 字符，必须是 execution root 下无 `..` 的相对路径；symlink/越界会失败，文件必须非空且相对 dispatch 前 snapshot 有变化。

输出：

```json
{"created": true, "harness": "pi", "job_id": 42}
```

### `run`

```text
run --workflow PATH
    [--once | --until-idle | --drain]
    [--drain-timeout-seconds N]
    [controller/worker selection options]
```

- 不给 mode：持续轮询；Ctrl-C 打印 `coordinator_stopped` 到 stderr 并正常退出。
- `--once`：执行一个受 `max_parallel` 与 replica slot 限制的 wave。
- `--until-idle` 与别名 `--drain`：多 wave 排空有效 worker pool；timeout 默认 3600 秒，CLI 范围 1–86400。
- `--once` 与 `--until-idle/--drain` 互斥。

单 wave JSON 同时保留兼容的顶层 state counts 和明确分组：

```json
{
  "pending": 0,
  "running": 0,
  "succeeded": 1,
  "blocked": 0,
  "failed": 0,
  "claimed": 1,
  "batch": {
    "pending": 0,
    "running": 0,
    "succeeded": 1,
    "blocked": 0,
    "failed": 0
  },
  "queue": {
    "pending": 3,
    "running": 0,
    "succeeded": 1,
    "blocked": 0,
    "failed": 0
  }
}
```

`--until-idle` 还输出 `mode`, `scope`, `idle`, `worker_pool_idle`,
`queue_idle`, `reason`, `waves`；`batch` 是各 wave 聚合。`idle=true` 退出 0；
blocked 或 drain timeout 导致 `idle=false` 时退出 1。若 CLI worker override 排除了
某些 queue job，可能出现 `worker_pool_idle=true` 但 `queue_idle=false`。

### `status`

```text
status --workflow PATH
```

输出：

```json
{
  "counts": {
    "pending": 0,
    "running": 0,
    "succeeded": 1,
    "blocked": 0,
    "failed": 0
  },
  "jobs": [],
  "workflow": "multi-harness"
}
```

每个 `jobs[]` 当前包含 id/title/harness/placement/state、attempt budget、agent/error、
execution path/workspace、receipt declaration、`agent_settled`、`task_verified` 与
`correlation_id` 的安全视图；不输出 prompt。

### `retry`

```text
retry --workflow PATH --job-id ID [--extra-attempts 1..10]
```

默认增加 1 次 budget，只接受 `failed` job。保留 job id、dedupe key 与已用 attempts，
把 state 置回 `pending` 并清除上次 error/verification/correlation current view。输出：

```json
{"attempts": 2, "job_id": 42, "max_attempts": 3, "state": "pending"}
```

### `resume`

```text
resume --workflow PATH --job-id ID --response-file PATH
```

只接受 `blocked` job 与非空 response 文件。它复用最近 receipt 的 agent/pane、原 placement
和同一 attempt，不重发原 prompt。输出 job/agent/pane/state、`agent_state`、error、
settlement/verification 和 queue counts。恢复为 `succeeded` 退出 0；仍为 `blocked` 退出 1。

### `gc`

```text
gc --workflow PATH
   (--succeeded-agents | --failed-agents)
   [--apply]
```

Scope 参数必选且互斥。默认 dry-run；只有 `--apply` 才关闭候选 pane。输出
`dry_run`, `candidate_count`, `candidates`, `actions`, `states` 与各类 skipped count。
Worktree、blocked、foreign/unowned、仍被活动 job 使用或缺少创建 receipt/pane identity
的 agent 不会进入候选。

### `dashboard`

```text
dashboard --workflow PATH
          [--host 127.0.0.1|localhost]
          [--port 0..65535]
          [--poll-seconds 0.25..60]
```

默认 `127.0.0.1:8765`，poll 2.0 秒；`port=0` 允许系统分配临时端口。非 loopback host
会被拒绝。启动后先输出：

```json
{"status": "dashboard_started", "url": "http://127.0.0.1:8765"}
```

随后阻塞提供只读 HTTP/SSE 服务；Ctrl-C 打印 `dashboard_stopped` 到 stderr 并正常退出。

### `smoke`

```text
smoke --workflow PATH
      [--harness droid|grok|codex|pi|claude|hermes ...]
```

`--harness` 可重复。命令对匹配 worker 做真实只读 turn，要求新增的
`HERDR-SMOKE-OK harness=<name>` output receipt，并清理由本次创建的临时 agent。输出：

```json
{
  "failures": [],
  "results": [
    {"harness": "pi", "state": "done", "task_verified": true}
  ]
}
```

所有目标都成功且结果数匹配退出 0，否则退出 1。

### `deliver`

```text
deliver --workflow PATH --goal-file PATH
        [--tracker-backend local-markdown|github]
        [--tracker-root PATH]
        [--github-repository OWNER/REPO]
        [--wayfinder auto|always|never]
        [--max-parallel 1..3]
        [--review-repair-rounds 0..2]
        [controller/worker selection options]
```

该命令本身就是 opt-in gate。成功 JSON：

```json
{
  "artifact_root": ".orchestrator/deliveries/...",
  "integration_branch": "ho/<slug>/integration",
  "integration_commit": "abcdef1",
  "review_rounds": 1,
  "run_id": "0123456789ab",
  "status": "succeeded",
  "tickets_completed": 2,
  "tracker_references": {"01": "..."}
}
```

成功只表示 isolated integration branch 完成并通过最终 gate；不会自动 push、PR、merge、
release 或 deploy。需要 secret/production principal-proxy decision 时退出 3，并保留 artifact。

## npm wrapper 的 setup 面

`package.json` 暴露的 Node wrapper 接受：

| 命令 | 参数与行为 |
| --- | --- |
| `install` | `--project PATH`, 可重复 `--harness`, `--install-skill`；安装/协调 manifest 管理文件 |
| `upgrade` / `update` | 与 install 参数相同，并支持 `--install-skill | --skip-skill`；保留用户修改 |
| `doctor` | `--project PATH` 加 runtime doctor 余下参数；组合 installation 与 runtime JSON |
| `uninstall` | `--project PATH`；只删除 hash 未变化的 manifest-owned 文件 |
| `--version` / `-v` | 输出 npm package version |

除 setup 命令外，wrapper 把 runtime 子命令和剩余参数转发给 Python，并自动注入已安装
workflow。Wrapper 参数或安装错误退出 2；runtime 退出码原样传递。

## 退出码总表

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令成功；或长运行被 Ctrl-C 正常停止 |
| `1` | 命令完成但健康/验收条件未满足：doctor、smoke、未排空 run、未成功 resume，或 npm reconciliation 检测到 preserved files |
| `2` | argparse、配置、catalog、store、transport、git、tracker、artifact 或一般 validation/dispatch 错误；消息在 stderr |
| `3` | `deliver` 的 protected-category principal-proxy escalation |

不要把 `blocked`、`unknown`、timeout 或单纯 agent settled 当成退出 0 的 task success。
