# Dashboard HTTP 与 SSE 契约
Active contributors: oldwinter, chendongdong

## Purpose

Dashboard HTTP/SSE 是 durable queue 与 Herdr runtime 的本机只读机器接口。它把 `src/herdr_orchestrator/dashboard/projector.py` 生成的最新 snapshot 暴露给浏览器或本地 automation；服务仅允许 loopback bind、仅实现 GET，并在 Host、静态资源和响应字段上使用白名单。

它不是 coordinator API：没有 enqueue、claim、retry、resume、focus、pane input、terminal output 或其他 mutation endpoint。HTTP health 只说明内存 feed 是否已有 snapshot，不证明 SQLite/Herdr 来源健康。

## 布局

```text
src/herdr_orchestrator/dashboard/
├── observer.py                         # SQLite/Herdr 字段白名单
├── projector.py                        # snapshot schema v1
├── server.py                           # bind、Host、routes、feed 与 SSE wire protocol
└── static/
    ├── index.html                      # GET / 页面
    ├── dashboard.css                   # /assets/dashboard.css
    ├── cytoscape.min.js                # /assets/cytoscape.min.js
    ├── topology.js                     # /assets/topology.js
    └── dashboard.js                    # /assets/dashboard.js

src/herdr_orchestrator/cli.py            # dashboard 命令与启动 JSON
docs/dashboard.md                       # 操作者接口摘要
tests/test_dashboard.py                 # HTTP/feed/Host/CSP/loopback 契约
```

## 关键抽象

### `DashboardServer`

`src/herdr_orchestrator/dashboard/server.py` 的 `DashboardServer` 组合 projector、`DashboardMonitor`、`SnapshotFeed` 和 `ThreadingHTTPServer`。构造参数边界：

| 参数 | 默认 | 合法范围 |
| --- | --- | --- |
| `host` | `127.0.0.1` | 仅 `127.0.0.1` 或 `localhost` |
| `port` | `8765` | `0..65535`；`0` 允许 OS 选择临时端口 |
| `poll_seconds` | `2.0` | `0.25..60` 秒 |

非法 host/port/poll 分别抛出 `dashboard_host_must_be_loopback`、`dashboard_port_invalid`、`dashboard_poll_seconds_invalid`；bind 失败折叠为 `dashboard_bind_failed`。

### `SnapshotFeed`

Feed 以 `threading.Condition` 维护：

- 当前最新 snapshot；
- 从 0 开始、每次 publish 加一的进程内 event ID；
- `current()` 当前值读取；
- `wait_after(id, timeout)` latest-value 等待。

Feed 不保存 event backlog。客户端落后多个 ID 时只收到当前最新 snapshot；Dashboard 重启后 event ID 重新从 0 开始。

### `DashboardMonitor`

Monitor 以 daemon thread 周期调用 projector，并在每轮都 publish，即使内容没有变化。projector 未处理异常不会结束 monitor，而是发布只含 source health 与异常类型的降级 snapshot。

### Request handler

每个 GET 的共同顺序是：Host 校验 → 路由 → 白名单资源/API 响应。未知路径返回 404；非法 Host 在路由前返回 421。handler 禁用默认 access log，但这不是认证或审计机制。

## 工作原理

```mermaid
sequenceDiagram
    participant P as RuntimeProjector
    participant M as DashboardMonitor
    participant F as SnapshotFeed
    participant C as Local client

    loop every poll_seconds
        M->>P: snapshot()
        alt success
            P-->>M: schema v1 snapshot
        else unhandled exception
            M-->>M: degraded snapshot
        end
        M->>F: publish(snapshot)
        F->>F: event_id += 1; notify_all
    end

    C->>F: GET /api/snapshot
    F-->>C: {event_id, snapshot}
    C->>F: GET /api/events + Last-Event-ID
    loop connection alive
        F-->>C: named snapshot event
        opt 15 seconds without newer publish
            F-->>C: : heartbeat
        end
    end
```

仓库前端 `src/herdr_orchestrator/dashboard/static/dashboard.js` 按“先 snapshot、后 EventSource”的顺序工作，并监听命名 `snapshot` 事件；它不会调用写接口。

## 监听与请求边界

### Loopback bind 与 Host header

仅绑定 loopback 还不足以抵御浏览器 DNS rebinding/错发虚拟主机请求，因此每个 GET 还要求：

1. `Host` header 必须存在且可解析；
2. hostname 必须严格等于 `127.0.0.1` 或 `localhost`；
3. port 可省略；显式提供时必须等于 server 实际监听端口。

失败返回 `421 Misdirected Request`。其他 loopback 写法、外部域名、错误端口和不可解析 Host 都不接受。服务没有登录层；其信任域仍是当前 OS 用户，不能暴露为远程服务。

### 响应安全头

JSON API：

```text
Content-Type: application/json; charset=utf-8
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

白名单静态资源：

```text
Cache-Control: no-store
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```

SSE：

```text
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

CSP 只设置在静态资源响应；JSON 与 SSE 不携带该 header。所有脚本和 Cytoscape bundle 都由本服务自托管，`connect-src 'self'` 只允许同源 snapshot/SSE。

## GET endpoints

所有 endpoint 都先经过 Host 校验。

| 路径 | 成功 | 尚未就绪 / 拒绝 |
| --- | --- | --- |
| `GET /` | `200 text/html`，`src/herdr_orchestrator/dashboard/static/index.html` | 非法 Host 为 421 |
| `GET /assets/<name>` | `200`，精确白名单 asset | 非白名单为 404 |
| `GET /api/health` | feed 有 snapshot 时 `200` | feed 无 snapshot 时 `503`，两者都有 JSON |
| `GET /api/snapshot` | `200` + snapshot envelope | 无 snapshot 时 `503` + `snapshot_not_ready` |
| `GET /api/events` | `200`，持续 SSE stream | 客户端断开后结束该 handler |

静态资源白名单只有：

- `index.html`
- `dashboard.css`
- `cytoscape.min.js`
- `topology.js`
- `dashboard.js`

### `GET /api/health`

已就绪：

```json
{"ok": true, "event_id": 12}
```

未就绪：

```json
{"ok": false, "event_id": 0}
```

`ok=true` 只意味着 feed 中已有值，包括降级 snapshot；来源健康必须检查 snapshot 的 `source_health`。

### `GET /api/snapshot`

成功 envelope：

```json
{
  "event_id": 12,
  "snapshot": {
    "schema_version": 1,
    "workflow": "multi-harness"
  }
}
```

尚未就绪：

```json
{"error": "snapshot_not_ready"}
```

SSE `data:` 直接是 snapshot 对象，不使用这层 envelope。

## Snapshot schema v1

### 正常顶层

```json
{
  "schema_version": 1,
  "workflow": "multi-harness",
  "generated_at": 2000.0,
  "source_health": {
    "queue": "ok",
    "herdr": "ok",
    "herdr_error": null
  },
  "summary": {
    "total": 2,
    "pending": 0,
    "running": 1,
    "blocked": 0,
    "failed": 0,
    "succeeded": 1,
    "active_agents": 1,
    "worktrees": 1,
    "needs_attention": 2
  },
  "jobs": [],
  "attention": [],
  "topology": {"workspaces": [], "projects": []},
  "timeline": []
}
```

| 字段 | 类型 | 契约 |
| --- | --- | --- |
| `schema_version` | integer | 当前固定为 1 |
| `workflow` | string | 当前 workflow name |
| `generated_at` | number | Unix epoch seconds |
| `source_health` | object | queue/Herdr 观察健康度 |
| `summary` | object | durable state 与运行拓扑计数 |
| `jobs` | array | durable job + 关联 runtime + drift |
| `attention` | array | 即时只读诊断项 |
| `topology` | object | 兼容 workspaces + additive projects |
| `timeline` | array | enqueue/receipt durable 事件，最多 100 条 |

### 降级顶层

Monitor 捕获 projector 异常时发布：

```json
{
  "schema_version": 1,
  "generated_at": 2000.0,
  "source_health": {
    "queue": "unavailable",
    "herdr": "unknown",
    "herdr_error": null
  },
  "error": "ExceptionType"
}
```

降级 snapshot 不伪造 `workflow`、`summary`、`jobs`、`attention`、`topology` 或 `timeline`。客户端必须容忍这些分区缺失；此时 `/api/health` 仍可返回 `ok=true`。

### `source_health`

正常 projector 把 queue 标为 `ok`。Herdr 为 `ok|unavailable`；transport 的稳定 error code 位于 `herdr_error`。Herdr unavailable 还会产生 `critical/herdr_unavailable` Attention，但 SQLite queue 仍可被投影。

### `summary`

所有值均为 integer：

| 字段 | 含义 |
| --- | --- |
| `total` | workflow job 总数 |
| `pending`、`running`、`blocked`、`failed`、`succeeded` | durable state 计数 |
| `active_agents` | Herdr `agent_status` 为 `working` 或 `blocked` 的 agent 数 |
| `worktrees` | `is_linked_worktree=true` 的相关 worktree 数 |
| `needs_attention` | Attention item 数，不是去重 job 数 |

### `jobs[]`

| 字段 | 类型 / 取值 |
| --- | --- |
| `id` | integer |
| `title`、`harness`、`dedupe_key`、`state` | string |
| `placement` | `pane|tab|worktree|null` |
| `attempts`、`max_attempts` | integer |
| `available_at`、`lease_until` | number 或 null |
| `agent_name` | string 或 null |
| `error_code`、`error_summary`、`correlation_id` | string 或 null |
| `agent_settled`、`task_verified` | boolean 或 null |
| `receipt_kind` | string 或 null |
| `execution_path`、`herdr_workspace_id` | string 或 null |
| `created_at`、`updated_at` | number，Unix epoch seconds |
| `runtime` | whitelisted agent object 或 null |
| `drift` | string array |

`runtime` 只可能含 `name`、`agent`、`agent_status`、`interactive_ready`、`state_change_seq`、`workspace_id`、`tab_id`、`pane_id`、`cwd`、`focused` 中上游实际提供的字段。

当前 drift code：

- `running_agent_missing`
- `terminal_job_agent_working`
- `workspace_mismatch`
- `lease_expired`

Snapshot 不包含 `jobs.prompt` 或 `receipt_value`。`error_summary` 在 durable store 写入前已经中央清洗，但仍是本地敏感诊断。

### `attention[]`

Job 相关项：

```json
{
  "severity": "warning",
  "code": "lease_expired",
  "job_id": 42,
  "title": "Task title",
  "message": "lease expired"
}
```

来源级项使用 `job_id: null`。当前生成规则：

| Severity | Code | 条件 |
| --- | --- | --- |
| critical | `herdr_unavailable` | Herdr observer 不可用 |
| critical | `job_blocked` | durable job 为 blocked |
| critical | `job_failed` | durable job 为 failed |
| warning | `<drift-code>` | `jobs[].drift` 中每个 code |
| warning | `job_stale` | running job 超过 300 秒无 durable state change |

同一 job 可同时产生多个项。Attention 不等于 `alerts.jsonl`，也不会触发控制动作。

### `topology`

`topology.workspaces` 是兼容视图；`topology.projects` 是 additive compound 视图：

```text
topology.projects[]
└── worktrees[]
    └── tabs[]
        └── panes[]
            └── agent object | null
```

`projects[]`：

- `project_id`: `workflow:<workflow-name>`
- `label`: workflow name
- `worktrees[]`

`worktrees[]`：

- `worktree_id`、`workspace_id`、`label`
- `path`、`branch`（可以为 null）
- `is_linked_worktree`
- `tabs[]`

一个相关 Herdr workspace 投影为一个 worktree/workspace 节点；没有 linked Git worktree 时仍可存在，path/branch 为 null，linked 为 false。

嵌套白名单：

| 实体 | 允许字段 |
| --- | --- |
| workspace | `workspace_id`、`label`、`focused`、`agent_status`、`tab_count`、`pane_count`、`active_tab_id`，另加 `tabs[]`、`worktree` |
| tab | `workspace_id`、`tab_id`、`label`、`focused`、`agent_status`、`pane_count`，另加 `panes[]` |
| pane | `workspace_id`、`tab_id`、`pane_id`、`cwd`、`focused`、`agent_status`、`interactive_ready`，另加关联 `agent object|null` |
| worktree | `path`、`branch`、`label`、`open_workspace_id`、`is_linked_worktree`、`is_detached`、`is_prunable` |

Herdr observer 仅纳入当前 workflow workspace 路径相关数据。terminal title、terminal ID、pane output 和未知字段不进入 snapshot。

### `timeline[]`

Timeline 将 job 创建事件和 attempt receipts 合并，按 `(at,id)` 倒序，最多 100 项。它是 durable lifecycle 投影，不是 SSE 历史。

Enqueue event：

| 字段 | 值 |
| --- | --- |
| `id` | `job:<job-id>:created` |
| `type` | `enqueued` |
| `at` | Unix epoch seconds |
| `job_id` | integer |
| `title` | string |
| `state` | `pending` |
| `attempt` | `0` |
| `agent_state`、`error_code` | null |
| `detail` | `<harness> · <placement-or-auto>` |

Receipt event 包含 `id=receipt:<receipt-id>`、`type=receipt`、`at`、`job_id`、`title`、`state`、`attempt`、`agent_state`、`error_code`、`correlation_id` 与 `<agent-name> · <placement>` detail。

## SSE wire protocol

### Snapshot event

每次 monitor publish 都增加 event ID 并发送完整快照：

```text
id: 12
event: snapshot
data: {"schema_version":1,"workflow":"multi-harness","generated_at":2000.0}

```

客户端必须监听命名 `snapshot` 事件；这不是默认 `message` 事件。服务不发送 patch，也不发送自定义 `retry:` 行。

### Heartbeat

`wait_after()` 15 秒没有更大的 event ID 时发送 SSE comment：

```text
: heartbeat

```

Heartbeat 没有 `id`、`event` 或 `data`，不能触发 snapshot render。

### `Last-Event-ID`

| 请求值 | 服务行为 |
| --- | --- |
| 缺失、非整数或负数 | 归一为 0 |
| 小于当前 ID | 立即发送当前最新 snapshot，不补中间帧 |
| 等于当前 ID | 等待下一次 publish；15 秒后 heartbeat |
| 大于当前 ID | 若已有 snapshot，立即以当前 ID + 当前 snapshot 重同步 |

浏览器 `EventSource` 断线后自动重连并携带最后 event ID。因为 feed 只有最新值，消费方应整体替换客户端投影；跨进程/跨重启审计应使用 snapshot timeline 和 SQLite receipt。

## 集成点

| 消费/生产方 | 接口 | 契约 |
| --- | --- | --- |
| `src/herdr_orchestrator/cli.py` | `dashboard --workflow ... --host ... --port ... --poll-seconds ...` | 启动 stdout 是 automation-friendly JSON，服务前台运行 |
| `src/herdr_orchestrator/dashboard/observer.py` | queue/Herdr observation | workflow scope、显式列/字段、无 prompt/terminal output |
| `src/herdr_orchestrator/dashboard/projector.py` | snapshot v1 | additive 演进；不要破坏旧 `topology.workspaces` 消费者 |
| `src/herdr_orchestrator/dashboard/static/dashboard.js` | snapshot fetch + named SSE | 容忍降级分区缺失；只消费最新完整值 |
| 本地 automation | health/snapshot/events | health 不代表 source healthy；SSE ID 不可作为 durable offset |
| `tests/test_dashboard.py` | server/feed contract | loopback、Host、CSP、asset、snapshot 与 event-ID 恢复 |

## 修改入口

| 目标 | 首要修改入口 | 必须同步 |
| --- | --- | --- |
| 新增/改变 snapshot 字段 | `src/herdr_orchestrator/dashboard/projector.py` | `schema_version`/additive 兼容、前端、`tests/test_dashboard.py`、本页 |
| 新增 SQLite/Herdr 字段 | `src/herdr_orchestrator/dashboard/observer.py` | 显式 whitelist、安全审查；禁止 prompt/terminal output |
| 改 health/snapshot envelope | `src/herdr_orchestrator/dashboard/server.py` | 状态码、no-store、客户端首屏与 API 文档 |
| 改 SSE replay/heartbeat | `src/herdr_orchestrator/dashboard/server.py` | latest-value vs backlog 语义、`Last-Event-ID`、断线测试 |
| 新增 route 或 asset | `src/herdr_orchestrator/dashboard/server.py` | Host 检查、精确白名单、content type、安全头、无写面 |
| 改 bind/Host 策略 | `src/herdr_orchestrator/dashboard/server.py` | 需要重新做认证、DNS rebinding、CSRF、连接预算与审计威胁建模；不能直接开放远程 |
| 改前端消费顺序 | `src/herdr_orchestrator/dashboard/static/dashboard.js` | 首屏/重连竞态、命名事件和降级 snapshot 容错 |

## Key source files

| 完整仓库路径 | 阅读重点 |
| --- | --- |
| `src/herdr_orchestrator/dashboard/server.py` | 参数校验、Host guard、route、headers、feed、monitor 与 SSE wire |
| `src/herdr_orchestrator/dashboard/projector.py` | snapshot v1 全部字段、drift、attention、topology、timeline |
| `src/herdr_orchestrator/dashboard/observer.py` | SQL/Herdr 白名单和 workflow/repo scope |
| `src/herdr_orchestrator/dashboard/static/dashboard.js` | 推荐客户端顺序、命名 SSE 监听和完整快照渲染 |
| `src/herdr_orchestrator/dashboard/static/topology.js` | `topology.projects` 与旧 `workspaces` 的客户端兼容 |
| `src/herdr_orchestrator/cli.py` | Dashboard CLI 参数和启动 JSON |
| `docs/dashboard.md` | 操作者启动、页面、安全与 endpoint 摘要 |
| `tests/test_dashboard.py` | feed 恢复、prompt 排除、projection、HTTP assets、Host/CSP/loopback |
| `tests/test_topology_js.py` | snapshot topology 在浏览器纯投影层的兼容 fixture |

## 交叉链接

- [API 索引](index.md)：CLI、Dashboard 与内部 Herdr transport 的接口分层。
- [本地 Dashboard](../systems/dashboard.md)：observer/projector/feed 与 Canvas 实现。
- [可观测性与 Attention](../features/observability-and-attention.md)：correlation、drift、telemetry 与 incident 流程。
- [Coordinator 与队列](../systems/coordinator-and-queue.md)：durable state、lease 与 attempt 真源。
- [任务收据与恢复](../features/receipts-and-recovery.md)：`agent_settled`、`task_verified` 和 receipt timeline。
- [安全与信任边界](../security.md)：loopback、Host、CSP、字段白名单和本地敏感数据。
