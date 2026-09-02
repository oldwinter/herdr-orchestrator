# CLI 机器接口契约
Active contributors: oldwinter, chendongdong

CLI 入口由 `src/herdr_orchestrator/cli.py` 定义。所有子命令都要求 `--workflow`，先加载 workflow，再调用 command handler。对自动化调用方而言，契约由三条通道共同组成：

- **stdout**：成功结果；多数一次性命令为一个完整 JSON 对象。
- **stderr**：参数用法、稳定错误 token、长运行命令的停止提示。
- **process exit code**：区分成功、可报告的业务未达成、输入/配置/transport 错误和 delivery escalation。

## 没有统一的顶层 JSON envelope

一次性命令直接输出 command-specific 对象，例如：

```json
{"created":true,"harness":"pi","job_id":9}
```

它不是 `{"result": ...}`。`result` envelope 只存在于内部 Herdr subprocess protocol。CLI 错误也只是 stderr 文本，并不保证 `{"error": ...}`。因此调用方应：

1. 收集 stdout、stderr 与退出码；
2. 仅对该命令声明为 JSON 的 stdout 做一次 JSON 解码；
3. 根据退出码解释同一 payload，尤其是 `doctor`、`smoke`、`resume` 与 `run --until-idle`；
4. 不把 stderr 的附加路径或说明当作固定全文，只在需要时读取首个稳定错误 token。

例外输出：

- `catalog --format text` 输出多行文本。
- `profile --format text` 只输出完整 profile context。
- 不带 `--once` 或 `--until-idle` 的 `run` 持续运行，不产生每轮 JSON。
- `dashboard` 先输出一个启动 JSON，随后持续服务。

## 命令分组

| 分组 | 命令 | 状态影响 |
| --- | --- | --- |
| 发现与就绪 | `catalog`、`profile`、`doctor`、`smoke` | 前两者读 catalog；后两者执行受限探测，不写任务队列 |
| Queue 输入与查询 | `seed`、`enqueue`、`status` | `seed`/`enqueue` 幂等写 queue；`status` 读取 durable state |
| 执行与恢复 | `run`、`retry`、`resume`、`gc` | claim/dispatch、显式重试、恢复原 blocked agent、清理本运行拥有的 terminal agent |
| 本地观察 | `dashboard` | 初始化数据库并启动只读 HTTP/SSE 投影 |
| 显式标准交付 | `deliver` | opt-in delivery 流程；不是普通 queue 命令 |

## JSON 输出形状

以下字段来自 `src/herdr_orchestrator/cli.py`、`src/herdr_orchestrator/runner.py` 和 `src/herdr_orchestrator/store.py`；未列为固定字段的嵌套 adapter 结果不应被猜测。

### Queue 输入与查询

#### `seed`

```json
{"added":2,"existing":1}
```

- `added`：本次新建的 seed job 数。
- `existing`：被 `(workflow, dedupe_key)` 去重的既有 job 数。
- 可重复调用；两者之和等于 workflow 中本次处理的 seed 数。

#### `enqueue`

```json
{"created":true,"harness":"pi","job_id":9}
```

- `created` 为 `false` 时，`job_id` 和 `harness` 指向同一 dedupe key 的既有 job。
- `harness` 是 `droid|grok|codex|pi|claude|hermes`。
- `--harness auto` 是默认值；controller 只负责选出候选 worker，coordinator 校验后才入队。
- `--placement auto` 是默认值；最终 durable placement 为 `tab|pane|worktree`。

Task receipt 二选一：

- `--receipt-prefix`：去除首尾空白后长度为 1–256，不得含 CR/LF。
- `--receipt-file`：去除首尾空白后长度为 1–500，必须是 execution root 下无 `..` 的相对路径。

声明 receipt 后，agent settled 仍不足以成功；验证结果必须为 `task_verified=true`。

#### `status`

```json
{
  "counts": {
    "blocked": 0,
    "failed": 0,
    "pending": 1,
    "running": 0,
    "succeeded": 2
  },
  "jobs": [],
  "harness_health": {"eligible": [], "records": []},
  "workflow": "example"
}
```

`counts` 总是为五种 durable state 提供整数计数。`jobs[]` 按 job ID 排序，当前字段为：

| 字段 | 类型/取值 |
| --- | --- |
| `id` | integer |
| `title` | string |
| `harness` | harness 枚举字符串 |
| `placement` | `tab|pane|worktree|null` |
| `state` | job state |
| `attempts`, `max_attempts` | integer |
| `agent_name` | string 或 null |
| `error_code`, `error_summary` | string 或 null |
| `execution_path`, `herdr_workspace_id` | string 或 null |
| `receipt_kind` | `output-prefix|file|null` |
| `receipt_value` | string 或 null |
| `agent_settled`, `task_verified` | boolean 或 null |
| `correlation_id` | string 或 null |
| `availability_reason` | pending 且 harness 当前不可选时的 stable reason；否则省略 |

`status` 不输出 job prompt。

### 执行、恢复与清理

#### `run --once`

一次 claim/dispatch wave 返回：

```json
{
  "blocked": 0,
  "failed": 0,
  "pending": 0,
  "running": 0,
  "succeeded": 1,
  "claimed": 1,
  "batch": {
    "blocked": 0,
    "failed": 0,
    "pending": 0,
    "running": 0,
    "succeeded": 1
  },
  "queue": {
    "blocked": 0,
    "failed": 0,
    "pending": 0,
    "running": 0,
    "succeeded": 1
  }
}
```

顶层五个 state 计数与 `batch` 相同，表示本 wave 记录的结果；`queue` 是返回时整个 workflow 的 durable 计数。即使某个 job 进入 `blocked` 或 `failed`，该 wave 仍是已成功执行的 CLI 操作，`run --once` 返回 `0`。

#### `run --until-idle` / `run --drain`

返回累计 wave 报告：

| 字段 | 契约 |
| --- | --- |
| 五个顶层 state 与 `batch` | 本次 drain 的累计 outcome 计数 |
| `mode` | 固定为 `until_idle` |
| `scope` | 固定为 `worker_pool` |
| `idle` | worker 候选池是否达到 idle |
| `worker_pool_idle`, `queue_idle` | 候选池与全 workflow queue 的独立判断 |
| `reason` | `queue_idle|worker_pool_idle|blocked|degraded_capacity|drain_timeout` |
| `waves`, `claimed` | wave 数与累计 claim 数 |
| `queue` | 返回时全 workflow state 计数 |

退出码仅在 `idle=true` 时为 `0`；`blocked` 或 deadline 到期等 `idle=false` 结果返回 `1`，但 stdout 仍是有效 JSON。`--drain-timeout-seconds` 的 CLI 合法范围为 1–86400。

#### 持续 `run`

未选 mode 时进入 `run_forever()`。每轮结果不写 stdout；收到 `KeyboardInterrupt` 后 stderr 输出 `coordinator_stopped`，进程正常返回 `0`。

#### `retry`

仅接受 failed job，并增加 1–10 次 attempt budget：

```json
{"attempts":2,"job_id":42,"max_attempts":4,"state":"pending"}
```

`attempts` 是已经发生的 claim 数，`max_attempts` 是增加后的上限。命令把 state 重置为 `pending`，同时清除 lease 和上次错误/验证/关联字段；它不会立即 dispatch。

#### `resume`

`--response-file` 必须存在且内容去除首尾空白后非空。命令恢复原 blocked job 的同一 agent、pane 和 attempt，不创建普通新 attempt：

```json
{
  "agent_name": "worker-name",
  "agent_settled": true,
  "agent_state": "done",
  "attempt": 1,
  "error_code": null,
  "job_id": 42,
  "pane_id": "workspace:pane",
  "queue": {
    "blocked": 0,
    "failed": 0,
    "pending": 0,
    "running": 0,
    "succeeded": 1
  },
  "state": "succeeded",
  "task_verified": true
}
```

只有返回的 durable `state` 为 `succeeded` 时退出 `0`，否则退出 `1`。

#### `gc`

必须显式选择 `--succeeded-agents` 或 `--failed-agents`；默认 dry-run，只有 `--apply` 才执行关闭。顶层对象包含：

- `dry_run`
- `candidate_count`
- `candidates[]`：每项含 `job_id`、`agent_name`、`placement`、`pane_id`、`state`
- `actions[]`：transport cleanup adapter 的逐项结果
- `states[]`
- `skipped_blocked`、`skipped_worktrees`、`skipped_unowned`、`skipped_active`

GC 排除 worktree、未被本 workflow 拥有的 agent、仍被 active job 使用的 agent；普通 blocked agent 不属于 succeeded/failed 清理目标。

### 发现、就绪与观察

#### `catalog --format json`

```json
{
  "schema_version": 1,
  "harnesses": [
    {
      "avoid_for": [],
      "best_for": [],
      "display_name": "Example",
      "harness": "pi",
      "profile_ref": "harness:pi",
      "strengths": [],
      "summary": "Example summary",
      "traits": []
    }
  ]
}
```

数组只含当前 workflow 启用 worker 的 compact profile，顺序跟随 worker 配置。实际 profile 列表字段按配置提供内容；示例空数组只用于展示 JSON 类型。

#### `profile <harness> --format json`

```json
{
  "schema_version": 1,
  "profile": {
    "avoid_for": [],
    "best_for": [],
    "context": "full execution context",
    "display_name": "Example",
    "harness": "pi",
    "profile_ref": "harness:pi",
    "strengths": [],
    "summary": "Example summary",
    "traits": []
  }
}
```

只有所选 harness 的完整 Markdown context 被加载。默认 text 格式仅输出 `context` 内容。

#### `doctor`

无论 readiness 是否全部通过，stdout 都是报告：

```json
{
  "checks": [],
  "ok": true,
  "summary": {
    "checks": 0,
    "failed": 0,
    "harnesses": [],
    "passed": 0,
    "readiness_ms": 0
  }
}
```

实际 `checks[]` 的公共字段为 `check`、`ok`、`value`；readiness check 使用 `status`、`error_code`、`error_summary`、`duration_ms`、`phase_timings_ms`。探测状态可包括 `ready`、`auth_required`、`model_invalid`、`timeout`、`unavailable`、`error`。输出还包含 additive `harness_health` projection，列出 status、eligibility、reason、source、age、expiry/cooldown 和 retryable count，不含 prompt 或 terminal output。全部 check 通过时退出 `0`，否则退出 `1`。`--probe-timeout-seconds` 必须为 5–300；可重复 `--harness` 只探测已启用 harness。

#### `smoke`

```json
{
  "failures": [{"error":"error_code_or_agent_state","harness":"pi"}],
  "results": [{"harness":"codex","state":"done","task_verified":true}]
}
```

所有被选 worker 都验证成功时退出 `0`，否则退出 `1`。Smoke 要求固定 output-prefix receipt；`idle`/`done` 但未验证仍计为失败。

#### `dashboard`

绑定成功后立即 flush 一行：

```json
{"status":"dashboard_started","url":"http://127.0.0.1:8765"}
```

随后进程持续服务。默认 host/port/poll interval 为 `127.0.0.1`、`8765`、`2.0` 秒；服务端进一步校验 host、port 与 poll 范围。HTTP 契约见 [Dashboard HTTP 与 SSE](dashboard-http-sse.md)。

### 显式标准交付

#### `deliver`

成功对象字段固定为：

```json
{
  "artifact_root": ".orchestrator/deliveries/run-id",
  "integration_branch": "ho/<slug>/integration",
  "integration_commit": "commit-id",
  "review_rounds": 1,
  "run_id": "run-id",
  "status": "succeeded",
  "tickets_completed": 2,
  "tracker_references": {"ticket-id":"reference"}
}
```

该命令是显式 opt-in gate。Delivery 需要用户处理的 escalation 返回退出码 `3`；普通 queue 命令不会自动进入这条流程。

## 状态枚举与成功含义

枚举真源是 `src/herdr_orchestrator/model.py`。

| 类型 | 值 | 含义 |
| --- | --- | --- |
| `JobState` | `pending`、`running`、`succeeded`、`blocked`、`failed` | durable queue 状态 |
| `AgentState` | `idle`、`working`、`blocked`、`done`、`unknown` | 当前 Herdr lifecycle 观察 |
| `PlacementTarget` | `tab`、`pane`、`worktree` | dispatch 拓扑 |
| `ReceiptKind` | `output-prefix`、`file` | 内容验证方式 |

关键判定：

- `blocked` 与 `failed` 都是 durable terminal state；blocked 只通过显式 `resume --response-file` 恢复原 agent。
- `idle`/`done` 只说明 agent settled。
- 声明 receipt 的 job 只有 `task_verified=true` 才能进入成功路径。
- `unknown`、timeout 或未满足 receipt 都不是成功。

## 退出码与错误通道

| 退出码 | 来源 | stdout/stderr 约定 |
| --- | --- | --- |
| `0` | 命令成功；或 `doctor`/`smoke` 全通过；或 drain idle；或 resume succeeded | 按命令输出 JSON/文本 |
| `1` | readiness/smoke 未通过、drain 未 idle、resume 未 succeeded | 通常仍有可解析 JSON stdout |
| `2` | argparse 用法错误；配置、catalog、store、transport、tracker、输入验证等受控错误 | argparse 输出 usage；handler 错误将 `str(exception)` 写 stderr |
| `3` | `DeliveryEscalation` | escalation 文本写 stderr |

参数解析发生在 handler 的异常捕获之前，但 argparse 自身同样以 `2` 表示用法错误。受控 handler 错误没有统一 JSON body，例如 token 可为 `drain_timeout_out_of_range`、`receipt_prefix_invalid`、`response_file_not_found: ...`。未被 `main()` 显式捕获的程序错误不属于这组稳定分类。

## 内部 Herdr subprocess protocol

`src/herdr_orchestrator/protocol.py` 是 coordinator/observer 到 Herdr CLI 的内部边界：

```json
{"id":"request-id","result":{"agents":[]}}
```

`run_json()` 的契约是：

1. 在指定 cwd、timeout 下执行 argv，并捕获 stdout/stderr。
2. timeout 映射为 `herdr_timeout`，进程无法启动映射为 `herdr_unavailable`。
3. 非零退出时尝试解析 stderr 的 `error.code`。code 必须匹配 `[a-z][a-z0-9_]{0,63}`；否则降级为 `herdr_command_failed`。
4. 零退出时 stdout 必须是 JSON object，且 `result` 也必须是 object；否则为 `herdr_invalid_response`。
5. 返回给内部调用方的是解包后的 `result`，不是整个 envelope。

`run_text()` 使用相同的 timeout、启动失败和非零退出映射，但成功时直接返回 stdout，不做 JSON 校验。`TransportError` 可携带 `code`、子进程 `exit_code`、可选 `summary` 与 `agent_settled`；这些是内部 adapter 信息，不是外部 CLI 统一错误 schema。

## 相关实现与测试

- `src/herdr_orchestrator/cli.py`
- `src/herdr_orchestrator/model.py`
- `src/herdr_orchestrator/protocol.py`
- `src/herdr_orchestrator/runner.py`
- `src/herdr_orchestrator/store.py`
- `tests/test_cli.py`
