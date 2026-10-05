# Orca headless 能力清单与 herdr-orchestrator 对照

## 范围与证据口径

- 调研日期为 2026-10-03。上游是 `gwmage/orca-orchestrator`，固定提交 `83e9e1f28fe1c5dc7d073c1799373cf982175eff`，不是完整 Orca 桌面应用。[上游说明][U-readme]
- 当前仓库比较基线是 `oldwinter/herdr-orchestrator` 的 `0d666f5cc06f0011ad4561003c35a4622784cf33`。本文只记录这个基线已有行为，不把主代理正在研究或后续实现的内容算作已交付。
- 已通读上游全部公开 CLI 分支、全部生产代码、README、`docs/usage.md`，并检查核心测试与 fake-agent smoke。下文是源码审阅结论；没有安装上游依赖、启动真实 agent 或运行上游测试，不声称已验证真实运行兼容性。
- “已接通”表示公开 CLI 执行链中存在实现；“仅库”表示可导入或经公开类调用，但 CLI 未接入；“仅声明”表示只见类型、配置、注释或提示词；“缺口”表示所审查提交没有对应实现。
- 当前映射区分普通 durable queue、显式 `deliver` 和 manual manager。delivery 的 DAG/proxy 不能当作普通 queue 的 DAG/mailbox；dashboard 的 runtime drift 不能当作 Git base drift。[当前架构][H-arch]
- 上游 `NOTICE` 将抽取来源标为 Orca 提交 `e4c7aab4a2df661431004c8048896cb428c5c072`。它不是本次审阅版本。引用或移植代码须保留 MIT 与 Lovecast 版权声明。[出处][U-notice]

## 结论

当前普通 queue 的主要缺口是持久化任务 DAG、worker 主动通信协议、thread/mailbox、带关联身份的问答与 decision gate、群地址，以及派发前 Git base drift 检查。现有 attempt fencing、恢复判定、completion 验证、readiness routing 比上游更严格，不能用上游的“已发送”“worker_done”或“心跳存在”替换。[当前 queue][H-store] [当前 completion][H-completion] [上游生命周期][U-life]

上游也不是这些能力的完整参考实现。群地址只是库函数；没有 `reply` 命令、自动 mailbox 注入、自动拆解或自动合并；没有 durable PTY 重连、lease fencing、run 级任务隔离。gate 与 ask 的组合存在重复执行窗口。下列矩阵逐项区分。[CLI 路由][U-cli-entry] [调度循环][U-coordinator] [runtime][U-runtime]

## 全部公开 CLI

实际入口只有 `send`、`check`、`ask`、`task`、`gate`、`run` 六个命令族，共八个叶命令。全部可带可选 `orchestration` 前缀。无参数打印用法并返回 0；未知命令或异常返回 1；没有专用 `--help` 分支。[入口][U-cli-entry]

| 命令与参数 | 上游实际行为与限制 | 当前对应或缺口 |
| --- | --- | --- |
| `send --to --subject [--type --body --priority --thread --payload --task-id --dispatch-id --files-modified --report-path --phase --db --from]` | 已接通。默认 status；校验八种 type，priority 最终由 SQLite CHECK 校验；payload 接受 JSON，专用 flags 覆盖同名字段。生命周期身份可从发送者 active/latest dispatch 补齐。只入库，不直接处理 completion 或唤醒 agent。[U-cli-send] | 无 worker-facing send；当前 completion 经 transport 解析 envelope/receipt，不是共享 DB mailbox。[H-completion] |
| `check [--wait --timeout-ms --all --db --from]` | 已接通。普通查询升序读取全部未读，先标 delivered/read 再输出 JSON；wait 每秒轮询，默认 600000ms，超时输出 `[]` 并成功退出。all 优先，最多 100 条历史，降序且不消费。[U-cli-check] | status/dashboard 观察 queue，不等价于 worker inbox；缺消息消费接口。[H-cli] [H-dashboard] |
| `ask --to --question [--options --task-id --timeout-ms --db --from]` | 已接通。创建 thread，发送 decision_gate，等线程回复或同 task 新 resolved gate，默认 10 分钟。输出纯文本，超时 exit 1。无 dispatch-id/thread 参数。[U-cli-ask] | 普通 resume 是 operator 回答已 blocked agent，worker 不能创建结构化问题；delivery proxy 属于独立授权模式。[H-resume] [H-proxy] |
| `task create --spec [--title --deps --db --from]` | 已接通。逗号分隔依赖；无依赖 ready，有依赖 pending；保存创建者。不暴露 parentId/displayName，不校验依赖存在、环或已完成情况。[U-cli-task] [U-db-task] | enqueue 有 prompt/dedupe/harness/placement/completion policy，但 NewJob 没有 deps/parent。[H-cli] [H-model] |
| `task list [--db]` | 已接通。输出所有 task 与最新 active dispatch 的 assignee/ID。parser 虽接受 status，分支没有传给查询，因此过滤无效。[U-cli-task] [U-db-task-query] | status 已有 scoped queue、attempt phase、receipt/health；缺普通任务图与 message handle。[H-status] |
| `gate list [--task-id --status --db]` | 已接通。按 task/status 过滤；options 是 DB JSON 字符串。[U-cli-gate] [U-types] | 无 gate 存储/CLI；blocked job/attention 不是 gate 列表。[H-store] [H-cli] |
| `gate resolve --id --resolution [--db]` | 已接通。不存在 ID 则失败；否则写 resolved 并无条件把 task 改 ready。无 resolver 身份、dispatch 校验、已解决保护或选项约束。[U-cli-gate] [U-db-gate] | resume 校验原 attempt/agent/pane，保持原 attempt，不可映射为简单重排队。[H-resume] [H-attempts] |
| `run --spec [--repo --db --agent --task ... --worktree --max-concurrent --poll-ms]` | 已接通。repo 默认 cwd，DB 默认 `.orch/orchestration.db`，agent 默认 claude；重复 task 创建独立任务。默认并发 4、poll 2000ms。信号置 stop；退出 dispose 本进程全部 PTY。completed exit 0，其余 exit 1。无总 deadline、agentArgs、worktreesRoot CLI flags。[U-cli-run] [U-coordinator] | seed/enqueue/run once/drain 已有；当前有 bounded drain、replicas、TOML，但没有兼容 orch CLI。[H-cli] [H-runner] |

db flag 覆盖 `ORCH_DB`，from flag 覆盖 `ORCH_HANDLE`。非 run 命令不创建 DB 父目录。worker 另收 `ORCH_WORKTREE`；环境值可以修改，不是认证凭证。[入口][U-cli-send] [环境注入][U-runtime]

## 生命周期、DAG 与 dispatch 关联

| 能力 | 上游事实与成熟度 | 当前对应或缺口 |
| --- | --- | --- |
| 状态模型 | 已实现 task 的 pending/ready/dispatched/completed/failed/blocked，dispatch 的 pending/dispatched/completed/failed/circuit_broken，run 的 idle/running/completed/failed；部分初始枚举正常路径不使用。[U-types] [U-db-schema] | 已有 JobState、AgentState、AttemptPhase，不能逐字映射；task completed 不等于 verified job succeeded。[H-model] [H-completion] |
| run 与主循环 | run 新建记录；每 tick 处理消息、gate、stale warning、ready dispatch、收敛。spec 只存上下文；phase 的 merging 仅声明，未执行。[U-coordinator] | 普通 Coordinator 已有 cycle/drain/forever；delivery 另有 run journal 与阶段推进。[H-runner] [H-delivery] |
| run scope | tasks/dispatch/messages/gates 无 run_id；循环读取整库。run row 不绑定 DAG，多 run 共库互相影响。[U-db-schema] [U-coordinator] | queue 有 workflow/canonical workspace 过滤、dedupe 与健康 scope。不可照搬全库收敛。[H-store] [H-health] |
| DAG promotion | 已接通，完成一项 task 后检查 pending tasks，全部 deps completed 才 ready；parent_id 只存层次，不聚合子任务结果。[U-db-task] [U-db-task-query] | 普通 queue 缺依赖边；显式 delivery 有 blocked_by、前置依赖校验、frontier 与 integration 后推进。[H-model] [H-delivery-dag] [H-delivery-edges] |
| DAG 异常处理 | 无未知依赖/环验证、依赖失败传播。创建时全部依赖已完成仍 pending，因为只由后续 completion 触发 promotion。[U-db-task] [U-db-task-query] | delivery 要求 blocker 先出现；普通 queue 未支持 DAG，不能宣称已有通用失败传播。[H-delivery-edges] |
| 自动拆解 | 仅注释规划。decompose 只确认至少一项 task，否则抛错；没有 LLM 拆解。[U-coordinator] | 已有 schema-validated planner，但只提出独立 job，不能提交依赖图或 shell。[H-planner] [H-model] |
| 并发与 terminal 分配 | 排除 coordinator、busy、断开或不可写 terminal；只计 dispatched tasks；无可用 terminal 时每 tick 最多创建一个。gate 释放 dispatch，不再占并发。[U-dispatch] [U-db-gate] | 已有 max_parallel/replicas；blocked/recovery/resume 保留 slot，provisioning 串行、等待并行。[H-runner] [H-store-claim] |
| dispatch record | ready task 才可派发；检查 terminal 无 active ctx，继承历史最大 failure_count，写 ctx 后改 task dispatched。[U-db-dispatch] | 已有 claim transaction、attempt/fence/lease owner/operation token，比 check-then-write 更强。[H-store-claim] [H-attempts] |
| worker_done 身份 | 检查 taskId+dispatchId、ctx 所属 task/assignee、最新 ctx 与双方 dispatched；重复已完成消息幂等返回 completed；旧 ctx 拒绝。[U-life] | 已有 job_id+attempt+fencing_token envelope 和内部 operation/lease fence；缺同等强度的主动消息接口，不缺基本关联标识。[H-completion] [H-attempts] |
| omission 自动补齐 | send 对四种生命周期消息从 active/latest ctx 推导缺失 ID；旧 worker 若省略 ID，可能绑定到同 terminal 的新任务。[U-cli-send] | structured-v2 身份 claim 后固定注入，不按当前 agent 名重新推断。[H-completion] [H-runner] |
| completion 证据 | 只存 completedBy/filesModified/completedAt；不验证文件、commit、检查、reportPath 或正文。Failed subject 的 worker_done 仍 completed，与 preamble 的失败约定冲突。[U-life] [U-preamble] | 已有 receipt-v1/structured-v2、freshness、路径/大小/重复 envelope 检查；task_verified 仍只证明机器契约。[H-completion] [H-arch] |
| heartbeat | 仅 dispatchId 定向更新 dispatched ctx，不核对发送者/taskId/最新 ctx；phase 留在 message payload，不投影进 dispatch。[U-life] [U-db-heartbeat] | 已有 lease/live sequence；缺 worker 主动、身份绑定的进度/心跳协议。[H-attempts] [H-recovery] |
| stale detector | 超过 10 分钟且过 dispatch grace，每 tick 日志警告；不 fail、取消或释放 slot。threshold 转成 SQLite 时间格式。[U-coordinator] [U-db-heartbeat] | 已有 bounded wait 与过期 lease reconciliation；worker heartbeat warning 仍缺失。[H-runner] [H-recovery] |
| escalation/熔断 | 按 taskId 找最新 ctx 并 fail；三次 circuit_broken/task failed，之前 ready 重派。send 失败同样计数；无 backoff，创建 terminal 失败/漂移拒绝不计数。[U-escalation] [U-dispatch] [U-db-heartbeat] | 已有可配置 max_attempts、指数延迟上限 60 秒、人工追加 retry budget；无 worker-facing escalation。[H-attempts] [H-retry] |
| escalation 关联缺陷 | 不核对 from/dispatchId/current ownership，重复或迟到消息可失败当前 retry；failDispatch 也不要求 ctx active。[U-escalation] [U-db-heartbeat] | outcome mutation 已有 ownership/fence；新增 escalation 不能只凭 job ID 写结果。[H-attempts] |
| 收敛 | 仅全库所有 task completed/failed 才停止，有 failed 则 run failed；blocked-only 只警告，失败依赖 pending 可无限等待。无总 timeout；completedTasks 仅含本次读到的完成消息。[U-coordinator] [U-convergence] | bounded drain 返回 blocked/degraded_capacity/drain_timeout，区分 pool/global idle；idle 不代表业务验证通过。[H-runner] |

## threads、mailbox、check、ask、reply、gates 与 groups

| 能力 | 上游事实与成熟度 | 当前对应或缺口 |
| --- | --- | --- |
| 持久 mailbox | SQLite messages 有随机 ID、单调 sequence、from/to、正文、type、priority、thread、payload、read/delivered_at。priority 只存储，不改变消费顺序。[U-db-schema] [U-db-messages] | receipts/observability 不是 mailbox；没有对应表或 worker API。[H-store] [H-observability] |
| 八种消息语义 | status 仅日志；worker_done/heartbeat reconciliation；escalation/decision_gate 有 handler；dispatch/handoff/merge_ready 被忽略但仍标 read。[U-cli-send] [U-coordinator] | 现有 typed outcome/journal 没有这些协作消息；上游类型也不等于已实现 handoff/merge。[H-model] [H-delivery] |
| delivered/read | 有独立字段与查询。check 同时写两者，coordinator 只写 read，ask thread reply 只写 read。[U-db-messages] [U-cli-check] [U-cli-ask] | 缺消息交付/消费状态；receipt_observed 是 attempt 阶段，不可充当 read bit。[H-attempts] |
| 消费原子性 | query、mark、stdout 分离；并发 checker 可重复读，标读后崩溃可漏显示。无 consumer lease、ack/replay cursor 或 exactly-once。[U-cli-check] [U-db-messages] | attempt transaction 可作约束参考，但并非已实现 mailbox 消费。[H-store] |
| 线程查询 | getThreadMessagesFor 过滤 thread/toHandle/afterSequence，ask 使用；不验证 reply sender/type，也不要求 unread。[U-db-thread] [U-cli-ask] | 缺 thread/request/reply 模型；correlation_id 是派发观测标识，不是线程。[H-model] [H-observability] |
| reply | 无 reply 命令；可手工 send --thread 回信，无按 message ID 推导收件人/线程。formatter 却提示不存在的 `orca orchestration reply`。[U-cli-entry] [U-cli-send] [U-formatter] | 无 worker reply；resume 控制原 blocked turn，不是跨 agent 回信。[H-resume] |
| 历史 inbox | check all 已接通；全局 inbox、单消息查询、type 过滤仅库可用。没有独立 inbox/thread/wait-for-type 命令。[U-db-messages] [U-db-thread] [U-cli-entry] | status/timeline 有任务历史，没有协作消息历史。[H-status] [H-dashboard] |
| push-on-idle | 仅库辅助与注释。undelivered 查询/formatter 存在，runtime/CLI 没有接线；formatter 未由包根导出。[U-db-messages] [U-formatter] [U-runtime] [U-exports] | 没有消息自动注入；现有 prompt/response 受 ownership 和 deadline 约束。[H-recovery] |
| ask 回复关联 | 优先返回同线程发给自己的第一条新消息，否则同 task 新 resolved gate 的最后一条 resolution。gate 不存 thread/message ID，不是问题级精确关联；线程回信让 ask 返回，却不解决已创建 gate。[U-cli-ask] [U-db-gate] | 缺问题对象；delivery proxy 的 question hash/decision/response journal 仅用于显式交付。[H-proxy] |
| ask/check liveness | preamble 称等待本身是 liveness signal，实际 CLI 等待循环不写 heartbeat。check 超时不改 task，ask 超时不 timeout gate。[U-preamble] [U-cli-check] [U-cli-ask] | 现有 deadline/lease 不依赖该承诺；新增等待语义需真实保活机制。[H-recovery] |
| gate 建立 | handler 只要求 taskId/question；createGate 写 question/options，完成 active ctx 并改 task blocked。不验证任务存在、dispatch/from、去重或任务是否已终结。[U-escalation] [U-db-gate] | 普通 blocked 不存结构化 question/options；attention 不能视为可回答问题。[H-attempts] [H-completion] |
| gate 解决/重派 | resolve 无条件 ready；后续 dispatch 附最后一项 resolved gate 的问答，可换 terminal。不是原 dispatch 继续执行。[U-db-gate] [U-dispatch] | resume 保持原 attempt/agent/pane，并用独立 operation sequence，语义更保守。[H-resume] [H-attempts] |
| timeout/多 gate | timeoutGate 仅库，且只改 gate 不恢复 task；无 timer/CLI。resolve 一个即 ready，其他 pending gate 下次 tick 才 re-block；无聚合约束。[U-db-gate] [U-coordinator] | 缺一等 gate 模型及多问题/超时/撤销策略；普通 blocked 仅人工 resume。[H-arch] |
| ask/gate 重入风险 | gate 释放 ctx 时原 worker 仍在 ask；terminal 被当空闲，可收到其他 prompt。resolve 使原 ask 返回，又允许 coordinator 重派同一 task；旧完成因 ctx 已终结被拒，未保证工作只执行一次。[U-cli-ask] [U-db-gate] [U-dispatch] [U-life] | 当前 blocked 保留 slot 是已有保护；问答应明确选择原 attempt resume 或新 attempt，不可同时执行两条路径。[H-store-claim] [H-resume] |
| 群地址解析 | 仅库。支持 `@all`、`@idle`、`@worktree:<id>`、`@claude`、`@openclaude`、`@codex`、`@opencode`、`@mimo`、`@gemini`、`@droid`；排除发送者，未知组空集。idle 由回调提供，agent 组按 title token 匹配，不读 catalog 身份。[U-groups] | 缺群寻址；worker pool/replicas 是调度过滤，不是广播组。[H-model] [H-runner] |
| 群发落库 | 未接 CLI；send to @all 只存字面收件人一条 row。注释所述逐接收者 row/read tracking 与共享 thread 需调用方自行实现。[U-groups] [U-cli-send] | 无对应能力；迁入 resolver 不能宣称广播完成。[H-store] |

## runtime、worktree、drift 与 agent catalog

| 能力 | 上游事实与成熟度 | 当前对应或缺口 |
| --- | --- | --- |
| runtime 接口 | CoordinatorRuntime 实际五个方法：send/list/create/wait/probeDrift，README 称六个不准确；dispose 是具体 runtime 额外方法。Coordinator 从不调用 waitForTerminal。[U-runtime-contract] [U-readme] [U-coordinator] | 已有 Dispatcher 与 HerdrTransport/HerdrLayout；上游接口计数不是新增能力。[H-runner] [H-transport] [H-layout] |
| PTY 会话 | node-pty 启动 shell `-lc <command>`，默认 200×50；合并 process.env。进程内 Map 保存会话/connected/exitCode/output，缓存最后 200000 字符，preview 500 字符。[U-pty] | Herdr 承载持久 PTY，coordinator 不拥有 node-pty；换 headless runtime 是产品选择，不是补通信协议的前提。[H-transport] [H-arch] |
| prompt 注入 | bracketed paste + 默认 500ms + CR，限制 1MiB，ESC 转 `<ESC>`。写完即返回 accepted，不观察 turn acceptance、startup trust/auth/composer readiness。[U-runtime] [U-paste] | 已有 bounded startup、interactive_ready、sequence acceptance、稳定 settled 与 fatal classifier，不应回退。[H-transport] [H-recovery] |
| idle/exit wait | 仅库。每 250ms 检查，默认 180s；idle 仅要求曾输出且静默 3s，exit 检查 connected=false；任意非 exit condition 都走 idle 分支。[U-runtime] | 读取 Herdr lifecycle 并确认稳定 settled；idle/done 仍不等于 verified。[H-transport] [H-completion] |
| worktree 创建/复用 | 无 selector 时每个新 terminal 建随机 wt ID、orch branch，基于 repo HEAD；并非每个 task 独立，因为 terminal 会复用。指定 selector 时所有 terminal 可共享同一 checkout。[U-runtime] [U-worktree] [U-dispatch] | 普通 worktree path/branch 按 workflow/task key 稳定派生，retry 可复用；delivery 每 ticket 独立 checkout。[H-layout] [H-delivery-dag] |
| worktree resolve/清理 | 仅识别本进程 Map 的 ID/path/branch 和 root，不能枚举并恢复现存 Git worktree。库有 create(baseRef)/remove(force)，CLI 未暴露；remove 不删除 branch，run 结束也不删 checkout。[U-worktree] [U-cli-entry] | 当前保留普通 worktree/branch；GC 只回收可证明归属的 settled pane，排除 blocked/worktree/foreign agent。[H-layout] [H-runner] |
| Git drift probe | 有 selector 才检查。先取 tracking upstream，退到 origin/HEAD；fetch 失败仍用缓存 refs；计算 HEAD..base 数量及最近 5 条 subject。无 upstream 则 null。[U-worktree] | 普通 queue 没有 fetch/behind dispatch guard；GitWorkspace 的 ancestry/clean/merge 验证主要属于 delivery，功能不同。[H-layout] [H-git] |
| drift 拒绝规则 | behind 严格大于 20 且无 override 才跳过，不创建 ctx、不烧 failure budget，后续 tick 重试。未传 selector 或 probe 抛错/null 都放行；默认 run 恰好不传 selector。[U-runtime-contract] [U-dispatch] [U-cli-run] | 缺任务 base freshness policy。dashboard 的 workspace_mismatch/lease_expired 是 runtime drift，不覆盖 Git 落后。[H-dashboard] |
| drift override/preamble | task spec 独立行 allow-stale-base:true 忽略大小写匹配并剥除第一处；代码围栏内也匹配。behind>0 注入提示，无结构化审批记录；检查的是 coordinator selector，不自动检查实际目标 terminal checkout。[U-runtime-contract] [U-dispatch] [U-preamble] | 无此 override；新增时不宜靠模型可编辑 spec 文本授权跳过检查。[H-model] [H-layout] |
| 声明式 agent catalog | 34 个 TuiAgent：claude、claude-agent-teams、openclaude、codex、autohand、opencode、mimo-code、pi、omp、gemini、antigravity、aider、goose、amp、kilo、kiro、crush、aug、cline、codebuff、command-code、continue、cursor、droid、kimi、mistral-vibe、qwen-code、rovo、hermes、openclaw、copilot、grok、devin、ante。[U-agent-types] | 稳定 allowlist 仅 droid/grok/codex/pi/claude/hermes；其余 28 个缺适配与验证，不能只加枚举。[H-model] [H-catalog] |
| catalog 字段消费 | 定义 detectCmd/alias/required/unsupported、launchCmd/platform、expectedProcess、五种 promptInjectionMode、draft/trust/ready signal。HeadlessRuntime 只使用 launch command/platform，统一 paste；其余大多只是从完整 Orca 保留的声明。[U-agent-config] [U-runtime] | compact TOML 描述 strengths/best_for/avoid_for，完整 md 按需注入；readiness 有实际探测、TTL/cooldown，不同于未消费 metadata。[H-catalog] [H-health] |
| 多 agent 选型 | run 全局一个 agent，task 无 agent 字段，也无 per-task router/availability probe；claude-agent-teams 仍调用 Orca wrapper，不能算完全脱离 Orca 的独立实现。[U-cli-run] [U-types] [U-agent-config] | 当前 planner/router 可在启用且 eligible pool 内逐任务选 harness，显式指定不 fallback。[H-planner] [H-health] |
| 可读任务标签 | DB derive title/displayName，单行规整与 80/160 字符上限，避免截断 UTF-16 高代理项；runtime title 却直接取 spec 前 40 字符，未消费这些字段。[U-display] [U-dispatch] | job title 与 visible label 已有，稳定内部 agent digest 与人类标签分离。[H-layout] [H-model] |

## boot recovery、持久化、失败预算与安全

| 能力 | 上游事实与成熟度 | 当前对应或缺口 |
| --- | --- | --- |
| SQLite schema/migration | node:sqlite 同步封装，WAL/NORMAL/busy_timeout=5000；schema v5，迁移事务保留 message sequence/索引，增 heartbeat/delivered/creator/title。[U-db-schema] [U-db-migration] [U-sync-db] | 已有标准库 SQLite、schema v9、向后迁移；新表不能替换现有 attempts/receipts/health。[H-store] [H-attempts] |
| 原子状态改变 | 只有 schema migration 显式 BEGIN/COMMIT；dispatch 的 check/insert/update、completion/promotion、gate 写入/释放/状态切换是多条 autocommit。注释称“同一事务”不等于真实事务。[U-db-migration] [U-db-task-query] [U-db-dispatch] [U-db-gate] | 当前 BEGIN IMMEDIATE、fenced updates 已存在；应将新关联状态并入它们。[H-store] [H-attempts] |
| 并发 coordinator | 无 leader lease/CAS fencing、active dispatch 唯一约束、run 排他锁。两个进程可能通过相同 check 后重复派发；同步方法只串行单进程调用。[U-db-schema] [U-db-dispatch] [U-coordinator] | 已有事务 claim 与 ownership token，旧 coordinator 只能写 stale audit receipt，不能改当前结果。[H-attempts] [H-store-claim] |
| boot recovery | runFromExistingRun 仅使用已有 run ID 执行循环，没有恢复进度状态；DB 记录可重读，但 PTY/worktree Map 不持久化。重启后 dispatched task 不会自动重认领，死 worker 只会过时警告。[U-coordinator] [U-pty] [U-worktree] | 已有 lease reclaim、同 attempt reconciliation、baseline/sequence/identity 校验与 attention；Herdr server/机器重启仍不承诺无缝。[H-recovery] [H-arch] |
| side-effect 崩溃窗口 | ctx 已写但 prompt 尚未发送、paste 已写但 CR 未发、worker 改文件但 completion 未处理，都无持久化 operation phase/恢复协议。dispose kill PTY 不回填 task 状态。[U-dispatch] [U-runtime] [U-cli-run] | 当前每个关键阶段持久化，accepted 模糊窗口进入 attention 不重发；fencing 仍不能撤销 agent 外部副作用。[H-recovery] [H-attempts] [H-arch] |
| retry 幂等性/预算 | failure_count 跨 ctx 累计，阈值硬编码 3；重复 escalation 可重复增计数。无 enqueue dedupe、外部副作用键、显式 CLI retry/reset/backoff；reset helpers 仅库且直接删除。[U-db-heartbeat] [U-db-reset] [U-cli-entry] | 现有 dedupe contract、bounded retry、attempt/operation 身份，不应改成消息到达次数预算。[H-store] [H-retry] [H-attempts] |
| sender 授权 | from flag/env 可任意设置；持 DB 路径者能直接写整库。worker_done 检查 handle 一致只防误关联，不是防恶意 worker 的权限机制。[U-cli-send] [U-life] [U-db-schema] | 当前 JSON schema/fencing 也不是进程沙箱；默认工具权限边界须保持，不能宣称 worktree 或 token 可隔离恶意 harness。[H-arch] |
| 不可信 shell/输入 | runtime 库接受 opts.command、agentArgs.join 空格后交 shell -lc；argv 未 shell-escape。CLI 部分数值仅 parseInt，无有限正数范围，NaN timeout 可无限等待 check。send payload 无 object/大小/exact-key schema。[U-runtime] [U-pty] [U-cli-check] [U-cli-send] [U-cli-run] | 当前固定 argv、workflow 有界值、planner/router exact-key allowlist；不应为兼容性开放模型提供 command/argv。[H-transport] [H-planner] [H-arch] |
| 安全与敏感数据 | prompt ESC 清理/1MiB 限制是终端输入保护，不是 prompt-injection 防御。DB 保存原始 spec/message/body/payload，runtime 继承整个环境并提供 raw output；无脱敏、保留期限或 secret/production gate。[U-paste] [U-db-schema] [U-pty] | 当前 observability/receipt 有脱敏与有界摘要；job prompt 仍在本地 runtime DB。默认外部副作用未授权，delivery secret/production 必须升级。[H-observability] [H-proxy] [H-arch] |
| 依赖/部署边界 | package 声明 Node>=22，使用内置 node:sqlite，唯一生产依赖 node-pty。无 Electron，但仍依赖 agent CLI/git/环境登录态；本文未验证各 Node 小版本兼容性。[U-package] [U-sync-db] | 当前 Python 3.12+ 标准库控制面与 npm launcher，依赖 Herdr session；纯 Node 无 Herdr 模式缺失，但不是同一路径的功能等价替代。[H-arch] |

## 库导出、运维与文档承诺核对

| 能力/承诺 | 上游实际范围 | 当前对应或缺口 |
| --- | --- | --- |
| 包根 exports | Coordinator/options/runtime contract、parseAllowStaleBaseFromSpec/threshold、OrchestrationDb、preamble、lifecycle reconciliation、groups、全部 core types、HeadlessRuntime、SessionRegistry、WorktreeManager、catalog/launch/isTuiAgent 与相关 types。[U-exports] | Python 模块可以导入，但没有兼容上游 TS 库的承诺；当前包根主要是版本信息。[H-init] |
| DB 公开库接口 | message insert/get/read/delivered/thread/history；task create/get/list/status；ctx create/get/complete/fail/heartbeat/stale；gate create/resolve/timeout/query；run create/get/update/active；idle handles、resetAll/resetTasks/resetMessages/close。getIdleTerminals 从消息历史推导，与 runtime 实况不同，Coordinator 未使用它。[U-db-messages] [U-db-thread] [U-db-task] [U-db-task-query] [U-db-dispatch] [U-db-heartbeat] [U-db-gate] [U-db-reset] | Store/AttemptLedger 已有 queue/receipt/claim/resume/health API，无 messages/gates 对等接口。[H-store] [H-attempts] |
| reset 与删除 | 仅库 helpers，多条 DELETE，不是 CLI、非事务、无授权/备份/归属检查；worktree remove 也仅库。[U-db-reset] [U-worktree] | 当前 GC dry-run/ownership gate 更窄，不能让兼容层隐含获得删除权限。[H-runner] |
| 自动合并/交付 | merge_ready 只是消息类型，coordinator 忽略；merging phase 未用，usage 明确由人审查/合并 worktree。[U-coordinator] [U-usage] | 显式 delivery 已有 ticket clean commit、receipt、integration merge、双轴 review/repair，默认不 push/merge 用户分支。[H-delivery] [H-delivery-dag] [H-git] |
| dashboard/manager/网络服务 | 无 dashboard、HTTP/SSE、daemon/RPC 服务或交互 manager；CLI 直接操作 SQLite，不能从完整 Orca UI 推导能力。[U-cli-send] [U-exports] [U-readme] | 当前有只读 dashboard 和独立 manual manager；两者不拥有 queue 成功判定。[H-dashboard] [H-arch] |
| worker 行为约束 | preamble 要求三句摘要、只发一次 done、五分钟 heartbeat、禁 AskUserQuestion、完成后 idle。都是提示词；没有强制正文非空或恰好一次上报。[U-preamble] [U-cli-send] | 当前 selected profile/prompt policy 同样不是安全沙箱；机器 receipt 与 operator 授权边界仍独立。[H-catalog] [H-arch] |
| dev CLI 名 | preamble 用 ORCH_CLI_NAME，或 devMode 选 orch-dev；npm bin 只声明 orch。formatter 仍硬编码 orca。[U-preamble] [U-package] [U-formatter] | 没有 orch-dev/reply 兼容入口；移植提示词前必须与真实 CLI schema 对齐。[H-cli] |
| worker CLI 可达性 | run 不为新 PTY 安装 orch 或加入 PATH，仅继承环境；按 README 的 node dist 入口运行不自动保证 worker 能解析 orch。smoke 的 fake agent 通过 ORCH_CLI_PATH 调用绝对脚本，绕过这个真实前提。[U-cli-run] [U-runtime] [U-pty] [U-smoke] [U-fake-agent] | 新增 worker-facing CLI 还需确保各 checkout/terminal 使用同一受控命令版本，不能只写 preamble。[H-transport] |
| README “每 task 隔离” | 实现是每个新 terminal 建 checkout，复用 terminal 复用 checkout；显式 worktree selector 还可让多个 terminal 共用 root。文档不能当每任务 fresh checkout 保证。[U-readme] [U-usage] [U-runtime] | 当前 topology 显式区分 tab/pane/worktree；checkout 隔离不等于安全隔离。[H-layout] [H-arch] |
| “直到所有任务完成” | gated、孤儿 dispatched、坏 DAG、未安装 agent 等状态可永久等待；仅 shell 启动成功不证明 CLI ready，run 无整体 timeout。[U-usage] [U-convergence] [U-runtime] | 当前有 drain timeout、health eligibility、attention；仍需报告 blocked，不能假称无人值守必成功。[H-runner] [H-health] |
| 测试证据 | core tests 覆盖 DAG 顺序/并发/三次熔断/gate/retry stale done/非 owner done、schema migration、groups/formatter/preamble；coordinator runtime 是 mock。smoke 用 fake claude 写 marker 并 send done，不是 34 harness 认证。[U-tests-coordinator] [U-tests-db] [U-tests-groups] [U-tests-preamble] [U-smoke] | 当前有 crash matrix/completion/transport/health 测试与真实 readiness 入口；不能用上游 fake smoke 代替 Herdr 实机验证。[H-arch] |

## 供集成方使用的缺口分组

以下是比较结果，不是本次实现承诺。主代理可据此划定范围；全部保留普通 queue 与显式 delivery 的授权分界。

1. 普通 queue DAG：增加显式依赖/父子语义与 ready eligibility，并定义未知依赖、环、已完成依赖、失败传播、动态加任务和重启重算。复用既有 transaction/claim，而非照搬上游 autocommit promotion。[U-db-task-query] [H-store-claim] [H-delivery-edges]
2. worker 通信：持久 message/thread/recipient delivery 与 send/check/history/reply；每条生命周期消息绑定已有 job/attempt/fence/operation。身份补齐、message retry/dedupe、ack 崩溃窗口和权限必须单独定义。[U-cli-send] [U-db-messages] [H-attempts]
3. ask/gate：持久化 request/thread/task/attempt 关联、问题/选项、resolver、timeout 与历史；明确解决后原 attempt resume 还是新 attempt。禁止 gate 释放 terminal 后原 worker 又恢复造成双执行。[U-cli-ask] [U-db-gate] [H-resume]
4. groups：resolver 与真实 fan-out 均需实现，使用可信 catalog/runtime identity 而非 terminal title；限定 workflow/workspace/owned terminals，不向其他运行广播。[U-groups] [H-model] [H-store]
5. drift：普通 queue 新增实际目标 checkout 的 base 观测与派发策略；区分 live fetch/缓存/未知、Git drift/runtime drift、显式 override。漂移拒绝不应消耗 attempt budget。[U-dispatch] [U-worktree] [H-layout] [H-dashboard]
6. 运行观测：worker heartbeat/phase/escalation 若加入，应走同一 fenced state transition；无需替换既有 boot recovery、readiness、retry/attention 和 completion 验证。[U-life] [U-escalation] [H-attempts] [H-health] [H-recovery]
7. 可选扩展而非核心缺口：28 种额外 harness、Node 库兼容层、无 Herdr headless runtime。上游只有命令声明，不提供这些 harness 全链路可用证据；不应以数量对齐代替可靠性。[U-agent-config] [U-runtime] [H-catalog]

## 固定提交证据索引

所有上游链接固定到指定 SHA，所有当前链接固定到本次比较基线。链接中的行号用于定位审查依据，不引用浮动 main。

[U-readme]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/README.md#L1-L82
[U-usage]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/docs/usage.md#L1-L66
[U-notice]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/NOTICE#L1-L28
[U-package]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/package.json#L1-L27
[U-cli-send]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/cli/index.ts#L14-L143
[U-cli-check]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/cli/index.ts#L147-L182
[U-cli-ask]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/cli/index.ts#L186-L251
[U-cli-task]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/cli/index.ts#L255-L286
[U-cli-gate]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/cli/index.ts#L288-L319
[U-cli-run]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/cli/index.ts#L323-L392
[U-cli-entry]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/cli/index.ts#L396-L433
[U-types]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/types.ts#L1-L83
[U-runtime-contract]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/coordinator.ts#L7-L71
[U-coordinator]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/coordinator.ts#L73-L292
[U-escalation]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/coordinator.ts#L294-L381
[U-dispatch]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/coordinator.ts#L383-L552
[U-convergence]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/coordinator.ts#L554-L585
[U-life]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/lifecycle-reconciliation.ts#L27-L149
[U-db-schema]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L37-L150
[U-db-migration]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L152-L283
[U-db-messages]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L285-L407
[U-db-thread]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L409-L426
[U-db-task]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L428-L465
[U-db-task-query]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L468-L567
[U-db-dispatch]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L569-L667
[U-db-heartbeat]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L670-L731
[U-db-gate]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L733-L810
[U-db-reset]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.ts#L812-L900
[U-sync-db]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/sync-database.ts#L1-L69
[U-groups]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/groups.ts#L1-L87
[U-preamble]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/preamble.ts#L1-L185
[U-formatter]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/formatter.ts#L1-L43
[U-display]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/orchestration-task-display.ts#L1-L53
[U-runtime]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/adapter/runtime.ts#L14-L180
[U-pty]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/adapter/pty-session.ts#L1-L153
[U-worktree]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/adapter/worktree.ts#L21-L141
[U-paste]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/adapter/prompt-injection.ts#L1-L42
[U-agent-types]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/agents/types.ts#L1-L36
[U-agent-config]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/agents/tui-agent-config.ts#L4-L382
[U-exports]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/index.ts#L1-L21
[U-tests-coordinator]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/coordinator.test.ts#L16-L885
[U-tests-db]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/db.test.ts#L21-L852
[U-tests-groups]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/groups.test.ts#L27-L181
[U-tests-preamble]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/src/core/preamble.test.ts#L25-L248
[U-smoke]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/scripts/e2e-smoke.sh#L1-L41
[U-fake-agent]: https://github.com/gwmage/orca-orchestrator/blob/83e9e1f28fe1c5dc7d073c1799373cf982175eff/scripts/fake-agent.cjs

[H-arch]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/docs/architecture.md
[H-cli]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/cli.py#L64-L277
[H-status]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/cli.py#L479-L526
[H-model]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/model.py#L21-L301
[H-store]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/store.py
[H-store-claim]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/store.py#L577-L802
[H-retry]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/store.py#L1224-L1279
[H-attempts]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/attempts.py
[H-recovery]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/attempt_runtime.py#L200-L540
[H-completion]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/completion.py
[H-runner]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/runner.py
[H-resume]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/runner.py#L488-L579
[H-planner]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/planner.py#L1-L154
[H-catalog]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/catalog.py#L1-L204
[H-health]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/harness_health.py
[H-transport]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/herdr.py
[H-layout]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/herdr_layout.py#L34-L398
[H-dashboard]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/dashboard/projector.py#L1-L190
[H-observability]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/docs/observability.md
[H-delivery]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/docs/standardized-delivery.md
[H-delivery-dag]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/delivery_recovery.py#L725-L852
[H-delivery-edges]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/delivery_protocol.py#L913-L921
[H-proxy]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/delivery.py#L1100-L1297
[H-git]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/git_workspace.py#L71-L244
[H-init]: https://github.com/oldwinter/herdr-orchestrator/blob/0d666f5cc06f0011ad4561003c35a4622784cf33/src/herdr_orchestrator/__init__.py#L1-L3
