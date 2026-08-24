# 架构与恢复语义

## 目标

首版解决一个问题：在 Herdr 提供的持久 PTY 上，由确定性 coordinator 持续向多个交互式 harness 派发不同类型任务，并在 coordinator 重启后恢复未完成工作。

## 组件

```text
Harness TOML metadata ─> compact catalog ─> Planner
                                              │ selected harness
Planner JSON / CLI enqueue ─> Controller router ─> Validator ─> SQLite queue
                                              │ claim
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
```

claim 在 `BEGIN IMMEDIATE` 事务内完成。`running` 任务持有 `lease_until`；coordinator 崩溃后，lease 过期任务可再次 claim。每次 claim 增加 attempt。

### Coordinator

- 一次最多 claim `max_parallel` 个任务；
- 每个 harness 最多同时占用 `replicas` 个 slot；默认 1，因此同 harness 任务默认串行；
- provisioning 后台 tab 与启动 agent 串行，避免布局竞争；
- agent prompt 可并发等待；
- `idle` / `done` 必须连续稳定 3 秒才成功；若期间重新进入 `working`，继续等待同一 deadline；
- 普通 queue 的 `blocked` 单独落 terminal 状态；
- settled 后检测少量、明确的 fatal runtime 信号（例如 Claude `/login` 加 401/403），避免把认证失败记成成功；
- `unknown`、timeout 和协议错误按失败与重试策略处理。
- 对必须写 strict JSON 的 turn，settled 后目标 artifact 缺失会在同一已 ready agent 上仅重发
  一次；artifact handshake 防止 startup lifecycle 变化被误认成任务完成。

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
- 只复用 kind 与 cwd 都匹配、且处于 settled state 的 agent；
- `agent start` 无论返回 `agent_not_ready`，还是成功 payload 暂时为 `working/unknown`，
  都进入有界 recovery wait 后再验证；
- 每个 harness 使用独立后台 tab 和 full-size root pane，始终 `--no-focus`，避免多 agent 连续 split 后 TUI 过窄；
- 所有 CLI 结果按 JSON schema 读取，不预测 pane ID；
- runtime output 只用于诊断摘要和少量确定性 fatal signal，不作为完整 transcript 或一般正确性证明。

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
schema 都要求 secret/production 升级，response 本身不进入 decision ledger。

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
