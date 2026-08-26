# Dashboard HTTP 与 SSE 契约
Active contributors: oldwinter, chendongdong

Dashboard 是 durable queue 与 Herdr runtime 的本地只读投影。HTTP 服务由 `src/herdr_orchestrator/dashboard/server.py` 提供，正常快照由 `src/herdr_orchestrator/dashboard/projector.py` 生成。它不是 coordinator：没有 POST、retry、resume、focus、terminal output 或 runtime 修改端点。

## 监听与请求边界

### Loopback bind

构造 `DashboardServer` 时只接受：

- `127.0.0.1`
- `localhost`

其他 host 直接产生 `dashboard_host_must_be_loopback`。port 合法范围是 0–65535，其中 `0` 可用于让操作系统选择临时端口；poll interval 合法范围是 0.25–60 秒。默认值为 `127.0.0.1:8765` 和 2 秒。

### Host header 校验

每个 GET 在路由前都验证 `Host`：

- header 必须存在且可解析；
- hostname 必须严格为 `127.0.0.1` 或 `localhost`；
- port 可省略；若提供，必须等于服务实际监听端口。

不符合条件时返回 HTTP `421 Misdirected Request`。例如外部域名、其他 loopback 写法、错误端口和不可解析的 Host 都不被接受。

Loopback bind 限制网络可达性，Host 校验降低浏览器 DNS rebinding/错误虚拟主机请求进入本地服务的风险；两者不能替代本机用户边界。

## 响应安全头

所有 JSON API 响应设置：

```text
Content-Type: application/json; charset=utf-8
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

静态资源同样设置 `Cache-Control: no-store` 与 `X-Content-Type-Options: nosniff`，并额外设置：

```text
Content-Security-Policy: default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
Referrer-Policy: no-referrer
```

CSP 位于静态资源响应；JSON 与 SSE 不携带这条 CSP。前端脚本和 Cytoscape bundle 均自托管，`connect-src 'self'` 只允许同源 snapshot/SSE 连接。

SSE 响应设置：

```text
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

## GET endpoints

所有路径都先经过 Host 校验。

| 路径 | 成功响应 | 尚未就绪/未知路径 |
| --- | --- | --- |
| `GET /` | `200`，打包的 `index.html` | 静态资源读取失败不定义为公共 JSON 错误 |
| `GET /assets/<name>` | `200`，白名单静态资源 | 非白名单为 `404` |
| `GET /api/health` | 有快照时 `200` | 无快照时 `503`；两者都有 JSON |
| `GET /api/snapshot` | 有快照时 `200` + snapshot envelope | 无快照时 `503 {"error":"snapshot_not_ready"}` |
| `GET /api/events` | `200`，持续 SSE stream | 连接断开即结束该 handler |

允许的静态资源名只有：

- `index.html`
- `dashboard.css`
- `cytoscape.min.js`
- `topology.js`
- `dashboard.js`

其他路径返回 `404`。服务没有写端点。

### Health

有快照：

```json
{"ok":true,"event_id":12}
```

尚未生成首个快照：

```json
{"ok":false,"event_id":0}
```

`ok` 只表示 feed 中已经有一个 snapshot，不表示 queue 与 Herdr 来源都健康。来源健康度必须读取 snapshot 的 `source_health`。

### Snapshot envelope

```json
{
  "event_id": 12,
  "snapshot": {
    "schema_version": 1
  }
}
```

`event_id` 是该进程中 feed 发布序号。SSE 的 `data` 则直接发送 snapshot 对象，不包裹 `event_id`/`snapshot` 外层。

## Snapshot schema v1

### 正常顶层

```json
{
  "schema_version": 1,
  "workflow": "example",
  "generated_at": 2000.0,
  "source_health": {
    "queue": "ok",
    "herdr": "ok",
    "herdr_error": null
  },
  "summary": {},
  "jobs": [],
  "attention": [],
  "topology": {
    "workspaces": [],
    "projects": []
  },
  "timeline": []
}
```

| 字段 | 类型 | 契约 |
| --- | --- | --- |
| `schema_version` | integer | 当前固定为 `1` |
| `workflow` | string | workflow 名 |
| `generated_at` | number | Unix epoch seconds |
| `source_health` | object | queue/Herdr 来源状态 |
| `summary` | object | state 与运行拓扑计数 |
| `jobs` | array | durable job + 关联 runtime |
| `attention` | array | 只读诊断项 |
| `topology` | object | 兼容 workspaces 视图与 projects compound 视图 |
| `timeline` | array | enqueue 与 receipt 生命周期，倒序最多 100 条 |

正常 projector 中 `source_health.queue` 为 `ok`；`source_health.herdr` 为 `ok|unavailable`，失败码位于 `herdr_error`。

### 降级快照

若 monitor 调用 projector 时发生未处理异常，它仍发布：

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

降级形状没有伪造空的 `summary`、`jobs`、`attention`、`topology` 或 `timeline`。客户端应先检查字段是否存在；此时 `/api/health` 仍报告 `ok: true`，因为 feed 已有快照。

### `summary`

所有值都是 integer：

| 字段 | 含义 |
| --- | --- |
| `total` | job 总数 |
| `pending`, `running`, `blocked`, `failed`, `succeeded` | durable state 计数 |
| `active_agents` | Herdr `agent_status` 为 `working` 或 `blocked` 的 agent 数 |
| `worktrees` | `is_linked_worktree=true` 的 worktree 数 |
| `needs_attention` | attention 项数 |

### `jobs[]`

| 字段 | 类型/取值 |
| --- | --- |
| `id` | integer |
| `title`, `harness`, `dedupe_key`, `state` | string |
| `placement` | `tab|pane|worktree|null` |
| `attempts`, `max_attempts` | integer |
| `available_at`, `lease_until` | number 或 null |
| `agent_name` | string 或 null |
| `error_code`, `error_summary`, `correlation_id` | string 或 null |
| `agent_settled`, `task_verified` | boolean 或 null |
| `receipt_kind` | `output-prefix|file|null` |
| `execution_path`, `herdr_workspace_id` | string 或 null |
| `created_at`, `updated_at` | number，Unix epoch seconds |
| `runtime` | 关联 agent object 或 null |
| `drift` | string array |

`runtime` 来自 Herdr agent 白名单；上游未提供的字段不会被补齐。允许保留的字段是：

`name`、`agent`、`agent_status`、`interactive_ready`、`state_change_seq`、`workspace_id`、`tab_id`、`pane_id`、`cwd`、`focused`。

当前 `drift` code：

- `running_agent_missing`
- `terminal_job_agent_working`
- `workspace_mismatch`
- `lease_expired`

Dashboard 不读取或返回 `jobs.prompt`。`receipt_value` 也不出现在 snapshot job 中。

### `attention[]`

每项固定为：

```json
{
  "severity": "warning",
  "code": "lease_expired",
  "job_id": 42,
  "title": "Task title",
  "message": "lease expired"
}
```

`severity` 当前为 `critical|warning`；与单个 job 无关的 Herdr observation 故障使用 `job_id: null`。生成规则：

- Herdr observation 不可用：`critical / herdr_unavailable`
- durable blocked：`critical / job_blocked`
- durable failed：`critical / job_failed`
- 每个 runtime drift：`warning / <drift-code>`
- running job 超过 300 秒没有 durable state change：`warning / job_stale`

一个 job 可产生多个 attention 项；`summary.needs_attention` 因而不是受影响 job 的去重数。

### `topology`

`topology.workspaces` 是 v1 兼容视图；`topology.projects` 是 additive compound 视图。

```text
topology.projects[]
└── worktrees[]
    └── tabs[]
        └── panes[]
            └── agent | null
```

`projects[]` 每项：

- `project_id`：`workflow:<workflow-name>`
- `label`：workflow 名
- `worktrees[]`

`worktrees[]` 每项：

- `worktree_id`
- `workspace_id`
- `label`
- `path`
- `branch`
- `is_linked_worktree`
- `tabs[]`

这里一个 Herdr workspace 投影为一个 worktree/workspace 节点；没有 linked Git worktree 时 `path`、`branch` 可为 null，`is_linked_worktree` 为 false。

`workspaces[]` 保留 workspace 白名单字段并增加 `tabs[]` 和 `worktree`。嵌套层的白名单如下；字段只在观察结果提供时存在：

| 实体 | 白名单字段 |
| --- | --- |
| workspace | `workspace_id`、`label`、`focused`、`agent_status`、`tab_count`、`pane_count`、`active_tab_id` |
| tab | `workspace_id`、`tab_id`、`label`、`focused`、`agent_status`、`pane_count`，另加 `panes[]` |
| pane | `workspace_id`、`tab_id`、`pane_id`、`cwd`、`focused`、`agent`、`agent_status`、`interactive_ready`，另加关联的 `agent` object 或 null |
| worktree | `path`、`branch`、`label`、`open_workspace_id`、`is_linked_worktree`、`is_detached`、`is_prunable` |

Herdr observer 仅纳入与 workflow workspace 路径相关的 workspace、agent 与 worktree；terminal title、terminal ID、pane output 和未白名单上游字段都不会进入 snapshot。

### `timeline[]`

时间线按 `(at, id)` 倒序并截断为最近 100 项。它是 durable job 创建记录与 receipt 的投影，不等于 SSE event 历史。

Enqueue event 字段：

| 字段 | 值/类型 |
| --- | --- |
| `id` | `job:<job-id>:created` |
| `type` | `enqueued` |
| `at` | Unix epoch seconds |
| `job_id`, `attempt` | integer；enqueue 的 attempt 为 0 |
| `title` | string |
| `state` | `pending` |
| `agent_state`, `error_code` | null |
| `detail` | `<harness> · <placement-or-auto>` |

Receipt event 字段：

- `id`: `receipt:<receipt-id>`
- `type`: `receipt`
- `at`, `job_id`, `title`, `state`, `attempt`
- `agent_state`, `error_code`, `correlation_id`
- `detail`: `<agent-name> · <placement>`

## SSE wire protocol

### Event

每次 monitor publish 都使进程内 event ID 加一，即使 snapshot 内容与上次相同。服务发送完整快照，不发送 patch：

```text
id: 12
event: snapshot
data: {"schema_version":1,"workflow":"example","generated_at":2000.0}

```

客户端应监听名为 `snapshot` 的 event；普通 `message` handler 不会替代命名事件 handler。

### Heartbeat

等待 15 秒仍没有比当前 ID 更新的 snapshot 时发送 SSE comment：

```text
: heartbeat

```

Heartbeat 没有 `id`、`event` 或 `data`，不应触发 snapshot 渲染。

### `Last-Event-ID` 与重连

请求可携带 `Last-Event-ID`：

- 缺失、非整数或负数按 `0` 处理。
- 小于当前 feed ID 时，立即发送**当前最新 snapshot**。
- 等于当前 ID 时，等待下一次 publish；15 秒无更新则 heartbeat。
- 大于当前 feed ID 时，立即发送当前 ID 与最新 snapshot，完成重新同步。

Feed 只保存最新 snapshot，不保存逐 ID backlog。因此落后多个 ID 的客户端不会收到中间快照。浏览器 `EventSource` 会在断线后自动重连并携带最后收到的 event ID；服务端不发送自定义 `retry:` 行。

Event ID 是内存状态，Dashboard 重启后从 0 重新开始。需要跨重启追踪生命周期时应读取 snapshot 的 durable `timeline`，而不是把 SSE ID 当作数据库序号。

### 推荐客户端顺序

1. `GET /api/health` 可选；只用于判断是否已有快照。
2. `GET /api/snapshot` 获取首屏和当前 envelope。
3. 打开 `EventSource("/api/events")`。
4. 对每个 `snapshot` 事件整体替换客户端投影。
5. 连接错误时保留最后快照并等待 EventSource 自动重连。

仓库前端 `src/herdr_orchestrator/dashboard/static/dashboard.js` 正是先 fetch snapshot，再订阅命名 SSE event；它不会从 Dashboard 发起任何写操作。

## 数据与安全边界

- SQLite observer 通过 URI `mode=ro` 和 workflow filter 读取显式列。
- 不读取 job prompt、环境变量、secret 或 terminal/pane output。
- Herdr observer 每次控制命令 timeout 为 10 秒，并只输出实体白名单字段。
- 所有 attention 和 drift 都是诊断，不会自动 retry、resume、close 或修改 job。
- HTTP 服务仅回环监听、仅 GET、无缓存；它没有认证层，因此仍应视为当前本机用户的只读接口，而不是可暴露的网络服务。

## 相关实现与测试

- `src/herdr_orchestrator/dashboard/server.py`
- `src/herdr_orchestrator/dashboard/projector.py`
- `src/herdr_orchestrator/dashboard/observer.py`
- `src/herdr_orchestrator/dashboard/static/dashboard.js`
- `docs/dashboard.md`
- `tests/test_dashboard.py`
