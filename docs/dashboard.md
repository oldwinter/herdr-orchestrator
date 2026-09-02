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
Dashboard 要求 `state_db` 已存在且为当前兼容 schema。它只读打开该数据库，不会创建或迁移
状态；首次运行请先使用 `seed` 或 `enqueue` 初始化数据库。
服务关闭时 `shutdown()` 会先唤醒并结束活动 SSE 连接，再停止监听。`shutdown()` 可以重复调用，
也可以在服务尚未进入监听循环时调用。

连接指示器在稳定 `Live` 状态保持静止。进入 `Reconnecting` 时保留一次 360ms pill
transition，重新连通时 dot 只播放一次 640ms pulse；后续普通 SSE snapshot 不重播。
`prefers-reduced-motion` 保留颜色与文字状态反馈，不播放 keyframe motion。
`source-warning` 警告区与连接标签相互独立。错误语义和 `aria-hidden` 同步切换。普通 motion
让警告区及其所占页面空间在 240ms 内一起展开或收拢，避免恢复时下方内容单帧跳位。快速反向
从当前 grid track 继续，不先跳到端点。`prefers-reduced-motion` 立即应用相同的最终布局。
警告内容在可见状态下变化时，`source-warning-clip` 会先锁定当前高度，再在 240ms 内过渡到
新的 intrinsic 高度。短消息和长消息都保持同一语义节点；过渡取消后会清理 inline 高度，避免
隐藏 warning 继续占据页面空间。`prefers-reduced-motion` 不创建这段空间过渡。

浏览器 transport、snapshot freshness 与最后一次成功内容分别维护。SSE 出错时连接标签显示
`Reconnecting`，transport 警告优先于 queue/Herdr source-health 警告；已有内容继续保留并显示
`Last snapshot`。EventSource 重新 `open` 只清除 transport 错误，并按最后一次 snapshot 重新投影
source-health 警告，不会重绘内容或把旧 snapshot 标成新数据。此时已有 snapshot 的标签恢复为
`Live`，没有 snapshot 时显示 `Connected` 与 `Waiting for first snapshot`。只有浏览器接受新的 SSE
snapshot 后，freshness 才恢复，时间标签才重新显示 `Updated`。并发的初始 HTTP snapshot 只在
尚无 snapshot 时填充内容；它不能覆盖已到达的 SSE snapshot，也不能清除一次 transport 恢复所
等待的 freshness。

## 页面

### Workflow summary

顶部指标来自 durable job state 与实时 Herdr observation：

- running、pending、succeeded；
- blocked、failed 或 runtime drift 构成的 attention；
- working/blocked Herdr agent；
- Herdr 原生 linked worktree，摘要主标签为 `Git worktrees`。该值只统计 native isolated
  checkout，区别于 Canvas 为每个相关 Herdr workspace 投影的 worktree 节点数。

### Work in flight

任务按 `pending`、`running`、`blocked/failed`、`succeeded` 分列。任务卡数据包含 harness、
placement、attempt、agent、pane/workspace location、`agent_settled`、`task_verified`、receipt
kind 与最后的截断错误摘要。

Dashboard 不推测任务完成百分比。`working` 只表示 Herdr 当前观察；`agent_settled` 也不
代表内容正确。声明 task receipt 的任务只有 `task_verified=true` 才会成功，未声明时该值
为 null。

在宽度不超过 760px 时，列标题下方显示四个原生按钮，顺序始终与看板列一致。每个按钮
直接定位到对应列；按钮、看板或列根节点上的左右方向键按当前顺序移动到严格相邻列。
任务卡内的链接、按钮与其他控件继续拥有自己的方向键，Dashboard 不拦截这些嵌套事件。
手动触控、滚轮或滚动条移动会把最近的可达列记录为当前列，末列使用浏览器实际可达的
clamped scroll stop。桌面宽度隐藏这组冗余导航并移出 tab 顺序。

窄屏看板还会投影当前列的内容状态。有任务的当前列继续使用整体 queue density 对应的固定
滚动高度；空的当前列收至 176px，让 Recent lifecycle 紧接空状态出现。首次渲染先无动画落到
真实高度，后续切列才在 240ms 内同步过渡 `height` 与 `min-height`。快速反向切列从当前帧接管，
每列纵向 scroll 继续按 key 保留。桌面不应用该高度规则；`prefers-reduced-motion` 立即应用同一
最终高度。

当前列 key 只存在于浏览器页面内。稳定列顺序的 SSE 更新保留横向 raw scroll、每列纵向
scroll 和当前 key；列顺序、移动 breakpoint 或窄屏宽度变化时则按 key 即时重新对齐，再
恢复焦点，避免焦点留在视口外。导航发起的平滑移动只在允许 motion 时使用，用户的 pointer、
touch 或 wheel 意图会取消其 ownership；`prefers-reduced-motion` 使用同一最终位置但不动画。
该 key 不写入 snapshot、receipt、URL、storage、topology 或服务端状态。

窄屏下 Attention 出现或清除会立即同步主区域的 DOM、视觉与 tab 顺序。若 Work in flight
或 Attention rail 正在视口内，Dashboard 会在移动 DOM 后用一次即时 page scroll 补偿保持
当前可见区块的 viewport 位置，并恢复仍连接的焦点元素。补偿不持久化、不平滑滚动；允许
motion 时仍保留 300ms 的小幅 order cue，`prefers-reduced-motion` 只应用最终稳定位置。

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
pane 上，不形成额外结构层。选择节点只显示白名单详情，不会修改 Herdr runtime；窄屏的可逆
viewport 聚焦规则见下文。

工具栏支持放大、缩小和适配全部节点。细指针继续使用鼠标/滚轮平移与缩放；粗指针会话默认
由页面拥有 Canvas 区域的触控，使页面可以纵向滚动。工具栏首个 Move 按钮可把触控交给图形
以进行平移和缩放，再次按下恢复页面触控。该偏好只保存在当前页面内，刷新后粗指针会话重新
从页面模式开始。SSE 状态刷新只更新节点样式；只有 project、worktree、tab 或 pane 结构变化
时才重新运行确定性的层级分组布局，并保留用户当前 viewport。
Canvas 同时维护一份屏幕阅读器可读的 DOM topology tree。

Canvas 获得键盘焦点后，方向键以可中断的平滑动画平移 viewport，`Shift+方向键` 增大平移
距离；连续快速输入从上一个目标累计。`+` 和 `-` 调整缩放，工具栏 Fit 与 `Home` 或 `0`
以同一动画适配全部节点。`Control+方向键` 按确定顺序选择节点，`Escape` 清除选择。初次渲染
与响应式 camera handoff 的 Fit 保持即时；`prefers-reduced-motion` 立即应用相同的平移、缩放和
Fit 终点。达到缩放上下限时，对应工具栏按钮会禁用；Canvas 键盘命令保留焦点并通过礼貌状态
播报已到达最小或最大缩放。被拒绝的边界命令不会改变 viewport ownership 或可逆聚焦状态。
没有渲染 topology 时 Fit 按钮保持原生禁用；此时 Canvas 上的 `Home` 或 `0` 会播报
`No topology to fit.`，程序化 Fit 调用保持静默。

窄于 600px 的 Canvas 会在选择节点时保存当前 viewport，并用 180-240ms 的可中断动画聚焦该节点；
动画时长按当前与目标 camera 的缩放比例和平移距离计算，短距离切换保持轻快，长距离切换降低
单帧跳跃。
节点 inspector 使用同一个可反向的视觉生命周期：进入保持 260ms，清除选择时以更快的 160ms
退出并在结束后离开布局。选择与 `aria-hidden` 语义仍同步改变；快速重新选择会从当前视觉帧反向，
不等待旧动画。`prefers-reduced-motion` 只保留 120ms opacity 反馈，不执行空间位移或裁切。
leaf 聚焦不会缩小当前 viewport，且保证所选节点标签至少以 9px 渲染。compound 聚焦优先把所选
frame、选择提示以及带标签的后代放在 inspector 上方；为保持完整分支可见，Canvas 上所选容器的
标签可以小于 9px，inspector 提供可读的容器身份。`Escape` 或点击 Canvas 背景会恢复保存的
viewport。选择后手动平移、缩放或执行适配时，用户操作优先，清除选择不会恢复旧 viewport。
叶节点聚焦会把最近 worktree 的标签纳入上下文；上下文联合边界在当前可读缩放下可容纳时保持完整
居中。窄到无法容纳时，在完整叶边界可见的范围内尽量靠近上下文中心，且不降低叶标签的 9px 下限；
可选边界异常或叶本身在固定缩放下无法容纳时才回退叶中心。
紧凑模式会把选中的 project、worktree 或 tab 标签，以及叶节点选择路径上最近 worktree 的标签，
放在其 compound frame 顶部中央，避免左对齐标签越过可见区域。overview 和非紧凑模式保持原有
标签位置。结构变化会取消旧的恢复目标，
`prefers-reduced-motion` 会直接应用同一最终状态。
Canvas 在紧凑与非紧凑尺寸之间切换时，overview ownership 决定 camera handoff。`auto` overview
进入紧凑尺寸且已有选择时，会先按当前紧凑容器适配，再把该 viewport 保存为聚焦 baseline；清除
选择会恢复这个紧凑 overview。`auto` overview 在聚焦或恢复期间离开紧凑尺寸时，也会立即按当前
容器重新适配，且不播放恢复动画。`user` overview 不触发自动适配；已有 focus capture 只按新容器
尺寸 rebase，空闲 viewport 则保持用户当前视图。

拓扑图构建是纯函数（无 DOM、无 Cytoscape 依赖），位于独立的
`static/topology.js`，在 `dashboard.js` 之前加载并暴露全局函数
（`stateClass`、`normalizedProjects`、`topologyGraph`、`topologyPresetPositions`、
`topologyNavigationOrder`、`topologySelectionDirection`、`topologyFocusViewport`、
`topologyRebaseViewportCapture`、`topologyId`）。它被 `tests/test_topology_js.py` 用 Node 直接求值并做 fixture
契约测试：compound 嵌套与状态 class、确定性布局与结构签名稳定性（状态-only
SSE 更新不移动节点）、v1 workspaces 回退投影、节点身份编码、选择顺序与 viewport
聚焦计算。没有 Node 时该测试自动 skip。独立的 `static/topology-style.js` 只生成 Cytoscape
样式，并由 `dashboard.js` 显式传入 compact 与 reduced-motion 状态；它不持有 camera 或 DOM 状态。

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

新事件到达时，页面会区分两种阅读状态。若最新事件仍在视口，新增事件保持可见，原有可见卡片
通过 220–420ms 的距离感知 FLIP 过渡到新 grid 位置；连续 snapshot 从当前视觉帧接管。若最新
事件已完全离开视口，Dashboard 以第一条可见历史事件为阅读锚点，重绘后立即补偿 page scroll，
保持该事件的 viewport top 不变。`prefers-reduced-motion` 保留同一阅读锚点和最终布局，但不创建
卡片位移动画。该 continuity 状态只存在于浏览器，不写回 snapshot 或 durable state。

## 数据与安全

SQLite observer 使用只读连接，查询白名单列：

- 不读取 `jobs.prompt`；
- 不读取 `jobs.receipt_value` 或 `receipts.error_summary`；
- 不读取环境变量或 secret；
- 不读取 terminal/pane output。Job 的 `error_summary` 只是 Store 写入的有界摘要，不是完整
  terminal transcript。

Herdr observer 只保留 topology 与 lifecycle 白名单字段。Workspace-scoped 返回行必须匹配请求
的 workspace，pane 还必须使用已返回的 tab，并且 agent/pane 的 `cwd` 必须位于当前仓库。
非对象响应行会令 observation fail closed。HTTP 静态资源设置 CSP，所有接口
`Cache-Control: no-store`。服务不会监听非 loopback 地址。

## HTTP interface

| 路径 | 说明 |
| --- | --- |
| `GET /` | Dashboard 页面 |
| `GET /api/health` | monitor 是否已有快照 |
| `GET /api/snapshot` | 当前完整快照与 event ID |
| `GET /api/events` | SSE snapshot stream |

Dashboard 目前不提供 POST、retry、focus 或 blocked response 操作。

SSE 客户端携带超前的 `Last-Event-ID` 时，服务会在首个快照发布后重新同步，不会永久等待该 ID。
