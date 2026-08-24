# Local Operations Dashboard

Dashboard 是 durable queue 的只读实时投影，不是第二个 coordinator。它把 SQLite 任务与
receipt、Herdr workspace/tab/pane/agent、原生 Git worktree 关联成一张本地 Web 图景。

## 启动

```bash
just dashboard

# 可调整本地端口和观察间隔
just dashboard --port 9000 --poll-seconds 1
```

启动后命令会输出 automation-friendly JSON：

```json
{"status":"dashboard_started","url":"http://127.0.0.1:8765"}
```

Dashboard 只允许绑定 `127.0.0.1` 或 `localhost`。默认每 2 秒生成一次快照，
浏览器通过 Server-Sent Events 异步接收；断线会自动重连。

## 页面

### Workflow summary

顶部指标来自 durable job state 与实时 Herdr observation：

- running、pending、succeeded；
- blocked、failed 或 runtime drift 构成的 attention；
- working/blocked Herdr agent；
- Herdr 原生 linked worktree。

### Work in flight

任务按 `pending`、`running`、`blocked/failed`、`succeeded` 分列。任务卡数据包含 harness、
placement、attempt、agent、pane/workspace location、`agent_settled`、`task_verified`、receipt
kind 与最后的截断错误摘要。

Dashboard 不推测任务完成百分比。`working` 只表示 Herdr 当前观察；`agent_settled` 也不
代表内容正确。声明 task receipt 的任务只有 `task_verified=true` 才会成功，未声明时该值
为 null。

### Attention

当前检测：

- durable `blocked` 或 `failed`；
- job 为 `running`，但对应 agent 不存在；
- job 已 terminal，但 agent 仍为 `working`；
- receipt workspace 与实时 workspace 不一致；
- lease 过期；
- running job 超过 5 分钟没有 durable state change；
- Herdr observation 不可用。

这些是只读诊断，不会自动重试、终止或修改 job。

### Herdr topology

只显示与 workflow workspace 相关的 Herdr workspace，包括该仓库的原生 worktree。
页面以可平移、缩放的 Canvas 白板展示
`project → worktree/workspace → tab → pane` compound graph；agent 名称与 lifecycle 状态显示在
pane 上，不形成额外结构层。点击节点只显示白名单详情，不会 focus、移动或修改 Herdr runtime。

工具栏支持放大、缩小和适配全部节点，鼠标/触控可平移与缩放。SSE 状态刷新只更新节点样式；
只有 project、worktree、tab 或 pane 结构变化时才重新运行确定性的层级分组布局，并保留用户
当前 viewport。
Canvas 同时维护一份屏幕阅读器可读的 DOM topology tree。

Snapshot v1 继续提供原有的 `topology.workspaces`，并以 additive 字段提供
`topology.projects`，其中嵌套 worktree、tab 与 pane。旧消费者无需修改。

渲染器使用自托管的 Cytoscape.js 3.34.1 Canvas build，不从 CDN 加载，也不增加浏览器运行时
网络依赖。vendored bundle SHA-256 为
`5141892eb19898946e5af8300e14cec15a63a22186a4ca56d76819a91e2a3fe6`，MIT notice 位于
`src/herdr_orchestrator/dashboard/static/cytoscape.LICENSE.txt`。选型比较见
[`research/topology-canvas-options.md`](research/topology-canvas-options.md)。Dashboard 仍不读取
pane output。

### Recent lifecycle

时间线来自 durable job 创建时间与 receipt。当前最多返回最近 100 条，页面显示最近 24
条。Dashboard 重启不会丢失这些 durable 证据。

## 数据与安全

SQLite observer 使用只读连接，查询白名单列：

- 不读取 `jobs.prompt`；
- 不读取环境变量或 secret；
- 不读取 terminal/pane output。

Herdr observer 只保留 topology 与 lifecycle 白名单字段。HTTP 静态资源设置 CSP，
所有接口 `Cache-Control: no-store`。服务不会监听非 loopback 地址。

## HTTP interface

| 路径 | 说明 |
| --- | --- |
| `GET /` | Dashboard 页面 |
| `GET /api/health` | monitor 是否已有快照 |
| `GET /api/snapshot` | 当前完整快照与 event ID |
| `GET /api/events` | SSE snapshot stream |

Dashboard 目前不提供 POST、retry、focus 或 blocked response 操作。
