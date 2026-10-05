# Herdr 与 Orca 编排

当前实现从 [Orca headless 功能清单](orca-capability-matrix.md) 提炼能力，保留 Python 标准库和现有 attempt/receipt 协议。它不是 `orch` CLI 的逐参数兼容版本，也不把两个运行时的数据库同步到一起。

## 选择入口

| 场景 | 入口 | 状态真源 |
| --- | --- | --- |
| Herdr 中无人值守、多 harness 调度 | `just enqueue`、`just run-once`、`just run-until-idle` | 本仓库 SQLite queue/attempt/receipt |
| Herdr worker 协作、依赖、问题、人工门禁 | `just orchestration` | 同一 workflow/workspace 的 SQLite |
| Orca 中原生 Run、Task、Dispatch、远程 worker 与消息 | `just orca` | 正在运行的 Orca，不写本仓库 queue |
| 完整工程交付、集成与双轴 review | 显式 `just deliver` | 独立 delivery journal |

Orca 命令不要求 Herdr pane。普通 queue 的执行仍要求 Herdr 就绪；`orca` 入口不是 queue 的 transport selector。所有本地命令都可通过 `uv run python -m herdr_orchestrator` 调用，不要求 `just`。

## 普通 queue 的 DAG

```bash
just enqueue codex "Implement parser" .orchestrator/parser.md parser-v1 \
  --completion-policy structured-v2
# 假设返回 job_id=1；实际使用上条命令返回的 ID。
just enqueue claude "Review parser" .orchestrator/review.md review-v1 \
  --completion-policy structured-v2 --depends-on 1
just orchestration task-list
just run-until-idle --drain-timeout-seconds 600
```

- `--depends-on` 可重复，最多 100 项。前置任务必须已存在，且 workflow/workspace 完全相同。新增任务只能依赖更早 ID，所以不能形成环；已有依赖不可编辑。
- 依赖属于 dedupe contract。同 key 重试但改依赖会返回 `dedupe_contract_conflict`，不会改变原任务。
- 只有前置任务 `succeeded` 且 `task_verified=true` 才能放行。legacy unverified success 不放行；使用 `structured-v2` 或已验证的 receipt-v1。
- 等待依赖/gate 不消耗 attempt，不占 slot。前置任务失败不会把后继伪装成成功或自动删除；后继保留 pending，operator 可以按原流程 retry 前置任务。
- 没有运行中任务且所选 pool 全部在等约束时，drain 返回 `idle=false`、`reason=dependency_or_gate_wait`，不把“无可派发”当作“完成”。
- 依赖只保证执行顺序，不自动 merge 前置 worktree。需要将分支成果集成后再执行下游时，使用明确的集成任务或显式 delivery。

## 消息、线程与确认

```bash
just orchestration send --from coordinator --to ho-codex \
  --key followup-1 --subject "Review scope" --body "Only review parser behavior."
just orchestration check --handle ho-codex
just orchestration ack --handle ho-codex --id 1
just orchestration check --handle coordinator --type question --timeout-seconds 300
```

示例 handle/ID 需要替换为 `task-list` 返回值。`send` 是持久入库，不会向任意终端直接打字。worker 在检查点主动 check；coordinator 在真正 dispatch 时注入具体命令与 claim 后的身份。

- type 支持 status、dispatch、worker_done、merge_ready、escalation、handoff、decision_gate、heartbeat、question、reply；priority 支持 normal/high/urgent。消费顺序始终 FIFO，不按 priority 抢占。
- check 默认不消费。进程崩溃后仍返回未确认消息；处理完才 ack，可重复 ack。一次 ack 中任一 ID 不属于该收件人则整笔回滚。
- 每页最多 100 条，使用 `--after` 翻页、`--all` 查已确认历史、`--thread` 查线程、重复 `--type` 过滤。过滤不会隐式确认其他消息。
- `--key` 是发送者/收件人/作用域内幂等键。相同 key 与内容返回同一消息；内容变化拒绝。
- `--to @all` 或六个 catalog harness 群地址，如 `@codex`，在当前 scope 内选择 running/blocked job 的 agent，排除发送者。首次发送冻结收件人集合和 thread；重复发送不会因为新 worker 加入而扩大群发。
- 本地数据库没有权威 live-idle 投影，不实现 `@idle` 或跨 worktree 群发；这些群地址可通过 Orca 原生入口使用。
- 生命周期 type 必须显式给出 `--job-id`、`--attempt-id`、`--token`，并匹配当前 running attempt、agent 与未过期 lease。没有 latest-dispatch 身份猜测。
- `worker_done`、heartbeat、merge_ready 都只作为协作消息。不续租，不改变 queue outcome，不代替 completion envelope，不授权 merge。

## 阻塞问答和 gate

worker 使用 dispatch 注入的 `ask` 命令，保留同一 job/attempt/token 与 `--key`。`ask` 将 question 持久化后有界等待。coordinator 回答：

```bash
just orchestration reply --from coordinator --to ho-codex \
  --id 1 --key answer-1 --body "Use option A."
```

只有原问题的收件人能通过此协议回复，并且目标必须是原发送者。reply 继承原 thread，精确关联原 message ID。超时 exit 1，返回原 message ID 和 `resend=false`。原 worker 仍运行且 lease 有效时，重复完全相同的 ask 可以等待原问题，不生成第二个请求。超时不标记 worker 失败，也不触发重派。等待不超过 3600 秒，应小于该 turn/lease 的剩余期限。

这与普通 queue 的 terminal `blocked` 不同。agent 已进入 blocked 时继续使用 `just resume JOB_ID RESPONSE_FILE`，不通过消息强行改状态。

派发前人工 gate：

```bash
just orchestration gate-create --job-id 2 --id design-review \
  --question "Choose implementation direction" --option A --option B
just orchestration gate-list
just orchestration gate-resolve --id design-review --resolution "A, preserve the public API"
```

gate 只能附加到尚未开始的 pending job。create 与 claim 在同一 SQLite 写锁规则下串行，不能在 claim 后补 gate 假装暂停。resolve 不把 running/blocked 改成 pending；重复同一答案幂等，改答案拒绝。答案会注入任务 prompt，作为数据而非额外权限。运行中的问题使用 ask，不能套用上游 resolve 后直接 ready 的双执行风险路径。

## 派发前 Git base 检查

```bash
just run-once --base-ref origin/main --max-base-behind 20
```

这是显式选用的本地 refs 检查，不自动 fetch，不把陈旧 tracking ref 声称为远端当前状态。超过阈值或 ref/仓库无法验证时，任务留在 pending 且不消耗 attempt。证据位于 `run_once.drift_deferred`，包含路径、ref、behind 和原因。

已有 worktree 检查实际稳定派生的任务 checkout；尚未创建的 worktree 检查其源 checkout。recovery 不受该过滤影响。每轮检查是 claim 前快照，不是防止外部 Git 修改的锁；不会自动 pull/rebase/reset。整个 pool 被阻挡时 drain 返回 `base_drift_wait`。没有指定 `--base-ref` 时保留原行为。

## Orca 原生入口

先用当前 Orca 的 `skills get orchestration` 获取版本匹配指南。以下命令只是示例，不会因为本文出现而自动执行。

```bash
just orca run-list
just orca worker-list --run RUN_ID --include-remote
just orca --apply run-create --objective "Implement and review the parser"
just orca --apply worker-start --spec "Self-contained task and acceptance criteria" \
  --agent codex --worktree current --run RUN_ID
just orca --apply --timeout-seconds 330 check --run RUN_ID --wait --timeout-ms 300000
just orca --apply reply --id MESSAGE_ID --body "Use option A"
just orca --apply worker-release --dispatch DISPATCH_ID
```

- `--apply` 和 bridge 的 `--timeout-seconds` 必须放在原生命令前。其他参数固定 argv 转发，不拼 shell，不执行模型提交的命令。
- 原生写操作，包括消费 check/ack，默认拒绝；`check --peek`/`--all` 和 list/show/read 不要求 apply。apply 只允许这次原生操作，不是生产或发布授权。
- 保留 Orca JSON 原始结构、exit code、Dispatch/Delivery/recovery 信息，不把 accepted 当成 completed。成功读取 Run 不等于真实 worker 生命周期验证。
- 总是使用选定的一个 executable。优先 `ORCA_CLI_COMMAND`，再 dev 环境，否则 macOS 用 orca，Linux 非托管环境用 orca-ide，避免启动 GNOME 屏幕阅读器。
- timeout/不可解析返回明确 unknown，不自动重试、不切换 host、不回退 Herdr。保留原生 request ID 后按 Orca recovery 协议查询。
- 不提供 reset、stop、abandon 或原始 terminal close 转发。release 由 Orca 自身检查 settlement authority。跨主控 `--environment`/`--pairing-code` 被拒绝；原生 `worker-start --on` 可选 worker 执行服务器，Run 仍归当前 Orca 所有。

## 覆盖与边界

| 上游能力族 | 本次处理 |
| --- | --- |
| task DAG、并发调度、失败预算 | 新增普通 queue 依赖；复用 coordinator/replicas/attempt budget |
| dispatch 身份、恢复、完成判定 | 保留当前更严格的 fencing、attention、structured-v2 |
| messages、thread、check/history、ask | 新增本地协议，并补齐 reply/ack/dedupe |
| decision gates | 新增派发前 gate；运行中问题走同 attempt ask，不重派 |
| groups | 本地接通 @all/六 harness；Orca 原生保留其余 group |
| worktree、PTY、prompt 注入、worker catalog | 复用 Herdr；Orca 原生使用自己的 worker-start、model/effort 与 catalog |
| stale base guard | 新增显式 local-ref pre-claim guard，不隐式联网 |
| heartbeat/escalation/phase | 可持久传达；不以 heartbeat 替代 lease，不做缺失心跳自动 kill |
| Run、远程 worker、资源回收 | 接入当前 Orca 原生协议，不复制旧 headless 协议 |
| 34 agent 声明、Node 类库、无 Herdr/Orca PTY runtime | 未移植；六种 Herdr harness 保持现有验证边界，其他 agent 由 Orca 管理 |

新增代码按行为重新实现，没有复制上游源码。完整来源和上游未接通/有风险的能力见[固定提交矩阵](orca-capability-matrix.md)。不宣称与上游所有库 API 一比一兼容。

Schema v10 只新增 dependency/mail/broadcast/gate 表，不重写旧 jobs、attempts 或 receipts。迁移在事务中完成。消息正文和 gate 答案留在 runtime DB，可能包含私有业务信息，不能写 secret 或提交 Git。handle/token 校验用于关联和防陈旧写，不能防同一 OS 用户直接修改 DB。本地 CLI 不是权限沙箱。
