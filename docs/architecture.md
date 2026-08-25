# 架构与恢复语义

## 目标

首版解决一个问题：在 Herdr 提供的持久 PTY 上，由确定性 coordinator 持续向多个交互式 harness 派发不同类型任务，并在 coordinator 重启后恢复未完成工作。

## 组件

```text
Harness TOML metadata ─> compact catalog ─> Planner
                                              │ selected harness
Planner JSON / CLI enqueue ─> Controller router ─> Validator ─> SQLite queue
                                              │ claim
                             Topology policy ───┤ tab | pane | worktree
Selected Markdown profile ── dynamic load ────┤
                                              ▼
                                     Herdr transport adapter
                                              │
                        ┌─────────┬─────────┬──┴────┬────────┐
                     Droid  Grok Build  Codex  pi  Claude  Hermes
```

### Workflow loader

解析 TOML，所有相对路径都相对于 workflow 文件所在目录。未知 harness、重复 worker、越界 timeout 和不存在的 prompt 都 fail closed。

### Durable store

SQLite 是运行状态真源。任务状态为：

```text
pending -> running -> succeeded
                   -> blocked
                   -> pending  (仍可重试)
                   -> failed   (耗尽重试)
blocked --显式 resume response--> succeeded | blocked
```

claim 在 `BEGIN IMMEDIATE` 事务内完成。`running` 任务持有 `lease_until`；coordinator 崩溃后，lease 过期任务可再次 claim。每次 claim 增加 attempt。

耗尽 attempt 的 `failed` job 可用显式 `retry` 在原 job id 和 `dedupe_key` 上追加有界 attempt
budget；`blocked`、`pending`、`running` 或已成功任务拒绝 retry。普通 queue 不自动回答
`blocked`；人工审查后可用显式 `resume --response-file` 回答原 agent。resume 必须匹配已记录的
agent 与 pane，保持原 attempt，不重发任务 prompt；失败或再次提问仍保持 blocked。

### Coordinator

- 一次最多 claim `max_parallel` 个任务；
- 每个 harness 最多同时占用 `replicas` 个 slot；默认 1，因此同 harness 任务默认串行；
- provisioning 后台 tab 与启动 agent 串行，避免布局竞争；
- agent prompt 可并发等待；
- `idle` / `done` 必须连续稳定 3 秒才算 agent settled；若期间重新进入 `working`，继续等待同一 deadline；
- 普通 queue 的 `blocked` 单独落 terminal 状态；
- settled 后跨 harness 检测有界 fatal runtime 信号，包括登录墙、device login、provider
  retry exhaustion 与 invalid model，命中后保留稳定错误码和截断摘要；
- enqueue 可声明 output-prefix 或 execution-root file receipt；声明后必须验证通过才能成功。
  output-prefix 只接受当前 turn 新增且不与 prompt 独立行歧义的输出，file receipt 必须在当前
  turn 新建或改变；分别记录 `agent_settled` 与 `task_verified`；
- `unknown`、timeout 和协议错误按失败与重试策略处理。
- 对必须写 strict JSON 的 turn，settled 后目标 artifact 缺失会在同一已 ready agent 上仅重发
  一次；artifact handshake 防止 startup lifecycle 变化被误认成任务完成。

`run_once` 输出保留兼容的顶层本波计数，并新增 `claimed`、`batch` 和结束时全局 `queue`。
`run_until_idle` 在有界 timeout 内重复 replica-limited wave，且把剩余 deadline 传给每个
dispatch，直到当前 worker pool 没有 pending/running/blocked。blocked 会立即返回
`idle=false`、`reason=blocked`。结果用 `worker_pool_idle` 与
`queue_idle` 明确区分所选 pool 和全局 queue；pool 外任务不会造成假死，也不会被误报为
全局排空。

### Execution topology

普通 queue 在 harness selection 之外有独立 topology decision seam。harness 决定“谁做”，
topology 决定“在哪里做”，两者不耦合：

```text
explicit task override
  -> worker default
  -> deterministic read/write rules
  -> bounded controller JSON for ambiguous tasks
  -> validated PlacementTarget
```

- `tab`：独立 full-size tab，共享 workflow checkout；
- `pane`：同一 `run_once` 批次共享一个 tab，每个任务有独立 pane 和 agent；
- `worktree`：Herdr 原生 `worktree create/open` 创建独立 workspace、branch 和 checkout。

内部 agent name 仍带短 digest，用于跨 workspace、replica 与重启保持唯一；用户可见的 tab
或 worktree label 使用截断任务标题，不暴露 digest。Pane 布局每次选择当前面积最大的
pane，并根据宽高决定向右或向下 split，避免机械地连续切窄。

Worktree path 与 branch 从 workflow 和 task key 稳定派生，retry 可恢复同一 checkout。
普通 queue 不自动 merge、关闭 workspace、删除 checkout 或删除 branch；保留状态供人工
审查。Standardized delivery 仍使用自己的 integration/ticket worktree DAG 和收据协议，
不与普通 queue topology 混用。

显式 GC 默认 dry-run。succeeded 与 failed 使用独立 scope，只回收名称、workspace、cwd、
placement 和 settled state 均能证明由当前 workflow 拥有的 agent pane；即使任务原
placement 为 tab，也不会关闭整个 tab。blocked、worktree 与 active/foreign agent 始终跳过。

### Local operations dashboard

Dashboard 位于 coordinator 之外：

```text
SQLite read-only observer ─┐
                           ├─> RuntimeProjector ─> SnapshotFeed ─> HTTP + SSE
Herdr whitelist observer ──┘
```

`RuntimeProjector.snapshot()` 是页面和测试共用的 interface。实现内部关联
`job → named agent → pane → tab → workspace`，并计算 durable/runtime drift。面向浏览器的
topology 另外以 additive projection 组织为
`project → worktree/workspace → tab → pane`，供 Canvas compound graph 使用；旧 workspace
projection 保留。
Dashboard monitor 独立运行；它失败、退出或断线不改变 coordinator、lease 或 receipt。

SQLite observer 不读取 `jobs.prompt`。Herdr observer 不读取 pane output，只投影
workspace、tab、pane、agent lifecycle 和 worktree 的白名单字段。浏览器通过 SSE
接收完整、幂等 snapshot；snapshot event ID 只用于当前 dashboard 进程的重连，durable
历史仍来自 job 与 receipt。

### Harness catalog 与按需 profile

- `profiles/harnesses/*.toml` 是紧凑 catalog 真源，只包含主控做选择所需的元数据；
- workflow 的 `[[workers]]` 决定本次可被选择的 harness 子集；
- `[planner].worker_harnesses` 或 CLI override 可以进一步收窄候选池；
- planner prompt 和输出 schema 只暴露该子集，且只注入 compact catalog，不注入完整 Markdown；
- planner 为每个子任务输出 `harness`；
- job 被 claim 后，coordinator 才读取该 harness 的 `.md` profile；
- 完整 profile 与 task packet 一起注入 worker，未选中的 profile 不进入该 turn；
- profile path 禁止绝对路径和 `..`，metadata、context 大小与字段均 fail closed。

### Herdr adapter

- 强制要求 `HERDR_ENV=1`；
- agent 名由 workflow、workspace 与 harness 稳定派生；
- 新 agent 通过 `herdr agent start ... -- <native args>` 使用 control plane 固定的最高
  自动化参数：Droid `--auto high`、Grok `--always-approve --permission-mode
  bypassPermissions`、Codex `--dangerously-bypass-approvals-and-sandbox
  --dangerously-bypass-hook-trust`、pi `--approve`、Claude
  `--dangerously-skip-permissions`、Hermes `--yolo --accept-hooks`；planner 与 task
  packet 不能覆盖或注入启动参数；
- Claude 首次 workspace trust 没有原生 bypass 参数；只有新建 Claude agent 的 detection
  output 同时包含 `Accessing workspace:`、`Quick safety check:`、
  `Yes, I trust this folder` 与预期 execution root 时，adapter 才发送一次 Enter，其他
  startup block、登录、secret、approval 与需求问题都不自动回答；
- 只复用 kind 与 cwd 都匹配、且处于 settled state 的 agent；
- `agent start` 无论返回 `agent_not_ready`，还是成功 payload 暂时为 `working/unknown`，
  都进入有界 recovery wait 后再验证；
- 新 agent 在固定 settle 窗口后必须重新读取并确认 `interactive_ready=true`；startup
  瞬态 `blocked` 会在 settle 后复查，只有持续 blocked 才成为任务阻塞；
- prompt submission 使用 Herdr 默认 `agent prompt --wait`，其内部 acceptance handshake
  仍必须观察到 `state_change_seq` 前进；command timeout 后先复查 sequence，已进入
  `working` 就继续等待，未前进则返回 phase-specific `prompt_acceptance_timeout`；
- `agent_prompt_stalled` 且仍停在原 idle sequence 时最多重发两次 Enter；仍没有 lifecycle
  变化则快速返回 `agent_turn_not_observed`，不占满整个 agent timeout；
- 每个 harness 使用独立后台 tab 和 full-size root pane，始终 `--no-focus`，避免多 agent 连续 split 后 TUI 过窄；
- transport 通过独立 Herdr layout adapter provision tab、批次 pane 或原生 worktree，
  并从 JSON response 读取 workspace/tab/pane ID；
- 所有 CLI 结果按 JSON schema 读取，不预测 pane ID；
- runtime output 只用于诊断摘要、确定性 fatal signal 和显式 output-prefix receipt，不作为
  完整 transcript 或一般正确性证明。

未声明 task receipt 的兼容任务在 settled 且无 fatal signal 时仍可成功，但
`task_verified = null`。声明 task receipt 后，`task_verified = true` 证明指定前缀或文件
存在；它仍只证明该机器契约，不代替更广泛的质量审查。运行诊断与本仓库真实演练形成的经验见
[`runtime-troubleshooting.md`](runtime-troubleshooting.md)。

### Planner

planner 是可选输入源，不是调度器。启用后，它只能把以下 JSON 写到配置的 runtime 路径：

```json
{
  "tasks": [
    {
      "title": "任务标题",
      "harness": "codex",
      "prompt": "任务契约",
      "dedupe_key": "稳定去重键"
    }
  ]
}
```

coordinator 校验 harness、字段长度、任务数量和去重键后才入队。JSON 不接受 shell command 字段。
planner 只能选择当前 workflow catalog 中的 harness。任务 dispatch 时 coordinator 动态加载所选 harness 的完整 profile。

主控 harness 与 worker harness 是两个独立选择：

- 主控可由 TOML 或 CLI 明确指定；未指定时，coordinator 从候选池中按固定优先级选择已安装 CLI；
- planner 为一批子任务选择 worker；
- 直接 enqueue 未指定 worker 时，主控执行一次受限 router turn，只能写 `{"harness":"..."}`；
- 显式 worker 直接入队，不启动 router turn；
- runtime worker override 同时限制 planner catalog、自动路由候选和本轮 queue claim，池外任务保留在 durable queue。

### Opt-in standardized delivery

标准交付是独立的显式 command/Skill surface，不改变普通 durable queue 的状态语义。
它仍由确定性 coordinator 拥有阶段推进：

```text
route -> wayfinder? -> spec + ticket DAG -> frontier worktrees
      -> committed receipts -> integration -> Standards || Spec review
      -> controller verdict -> bounded repair -> complete
```

- `wayfinder=auto` 只有在单 session 装不下且仍有 decision fog 时才进入 map；
- map 只保存 decision questions/resolutions，清晰后返回 spec，不直接 implement；
- tickets 是 dependency-ordered tracer-bullet vertical slices；
- 一次最多执行 3 张 frontier tickets，每张有独立 branch、worktree、fresh agent context；
- worker 必须提交 clean commit，并给每条 acceptance criterion 写证据；
- coordinator merge 后才关闭 ticket，保证 blocker frontier 能继续推进；
- 最终只运行一次双轴 review；repair 后重新 review，最多 2 轮；
- reviewers 使用独立 agent names，明确禁止 review recursion 与再派发；
- controller 对 finding 引用作裁决，prose finding 不直接触发修改。

标准交付中的 `blocked` 会读取有限 worker detection output，让独立 proxy controller
输出严格 decision JSON，再把回答交回原 worker。最多 8 轮。deterministic guard 与
schema 都要求 secret/production 升级。由于 Herdr 拒绝向已 blocked agent 提交普通
`agent prompt`，response 通过受控的 pane literal text 加 agent Enter 输入，再按新的
lifecycle sequence 等待结果；response 本身不进入 decision ledger。

每次运行的状态、map、plan、routes、receipts、reviews、ledger 和 worktrees 保存在
`.orchestrator/deliveries/<run-id>/`。最终产物是隔离 integration branch 与 commit，
不会自动 push 或 merge 用户 branch。

## 24/7 边界

Herdr detach 不终止 coordinator 与 agent 进程，因此适合长时间运行。以下情况不承诺无缝：

- Herdr server 完整重启会终止 pane 进程；
- harness 原生 session 是否可恢复取决于对应 Herdr integration；
- 机器睡眠、重启或网络中断可能让任务 lease 过期后重跑；
- worktree、端口、数据库和 credential 仍可能跨任务共享。

因此任务必须可重试，或者用 `dedupe_key` 与外部系统幂等键保护副作用。默认策略不授权任何外部副作用。
