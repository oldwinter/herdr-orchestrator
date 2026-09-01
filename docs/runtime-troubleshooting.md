# Runtime troubleshooting and learned contracts

本文记录真实多 harness 演练暴露的运行契约、诊断步骤和已经固化的防回归规则。

## 四层证据

不要把以下状态混为一谈：

1. **Provisioned**：后台 tab、pane 和 harness 进程已经存在。
2. **Interactive ready**：`herdr agent get` 返回 `interactive_ready=true`，agent 可以接收输入。
3. **Turn observed**：提交 prompt 后 `state_change_seq` 前进，通常先进入 `working`。
4. **Settled**：同一 turn 最终稳定返回 `idle`、`done` 或 `blocked`。

第 3、4 层成立只证明 `agent_settled=true`。`done` 不证明输出内容正确；需要机器验收的任务
应 enqueue `--receipt-prefix` 或 `--receipt-file`，并要求 `task_verified=true`。

## Topology 证据

`just status` 的 job 项会包含 `placement`、`execution_path`、`herdr_workspace_id`、
`current_attempt_id` 和 `attempt_phase`。用户可见标题与内部 agent identity 是两套字段：标题不带 hash，
agent name 可带短 digest。诊断时不要根据 tab 标题推断 agent identity。

- `pane`：同一批次 tab 可包含多个 agent pane；
- `tab`：一个任务一个 tab；
- `worktree`：一个任务一个 Herdr worktree workspace。

原生 worktree 默认保留。用 `herdr worktree list --cwd <repo>` 查看，不要把“任务完成”
理解成 coordinator 已 merge 或 remove checkout。

## 2026-08-24 演练经验

一次六 harness 并发只读演练暴露了两个 startup 边界：

- 新 Grok 进程已创建，但第一次 prompt 没有形成可证明的 turn，等待完整 timeout 后回到
  `pending`；第二次复用已就绪 agent 后成功。
- 新 Claude 在 startup 瞬态被检测为 `blocked`，任务 prompt 尚未提交就被记为 terminal
  blocked；稍后直接读取该 agent 时已经 `interactive_ready=true` 且 idle。

由此得到以下规则：

- 进程存在不等于 agent 已经开始任务。
- 新 agent 不能靠固定 sleep 判 ready，sleep 后必须重新读取 `interactive_ready`。
- startup 的单次 blocked snapshot 不是用户问题；settle 后仍 blocked 才是。
- prompt 返回 settled snapshot 时，sequence 没有前进必须拒绝成功。
- prompt submission 与 task execution timeout 应分开；前者应快速确认或快速失败。
- 已 blocked agent 会拒绝普通 `agent prompt`；代理回答使用一次 `pane run`，由 Herdr 原子发送
  literal text 与 Enter。之后等待新的 sequence，不能复用普通 prompt 路径。
- retry receipt 必须保留每个 attempt，不能用最终成功覆盖第一次 timeout。
- 后台 tab 使用 `--no-focus` 是正确行为；“当前 pane 没看到活动”不是运行证据。

## 当前防回归行为

- 新 agent 固定 settle 后，有界等待 `interactive_ready=true`。
- startup 瞬态 blocked 会复查；普通 turn 中的 persistent blocked 仍是 terminal，且
  `run --until-idle` 返回 `idle=false`、`reason=blocked`。
- prompt 使用 Herdr 默认 `--wait`；内部 acceptance 仍要求新的 lifecycle sequence。command
  timeout 后先复查 sequence，已前进则继续，未前进返回 `prompt_acceptance_timeout`。
- stalled prompt 最多重发两次 Enter；仍无变化返回 `agent_turn_not_observed`。
- 进入 `working` 后继续使用 workflow 的 `agent_timeout_seconds` 等待 settled。
- principal proxy 使用一次 `pane run` 回答 blocked worker，再等待新的 lifecycle sequence；
  control response timeout 或不可解析时仍复查 live sequence。已接受但未 settled，或无法完成
  reconciliation 时，同一 operation 进入 attention；普通 queue 不自动回答，也不旋转 token 重发。
- 人工审查后，普通 queue 可用显式 `resume --job-id ... --response-file ...` 回答；命令验证
  原 agent、pane 与 execution workspace，并保持原 attempt。每次 resume 使用新的 operation
  token；过期且已接受但尚未 durably settled 的 response 进入 attention，不会重复发送。
- prompt 接受前的 `unknown`、timeout、未观察到 turn 和协议错误不会记成功，并可按 budget
  重试。prompt 接受后若 turn 仍可能运行，job 进入 `blocked`，`attempt_phase` 为 `attention`。
- lease 过期后先恢复原 attempt。`claimed` 尚未取得 runtime，或有 durable baseline 的
  `runtime_acquired` live snapshot 仍等于 baseline 时，coordinator 才能证明输入未接受并
  abandon 该 operation；旧 schema 缺失 baseline 时进入 attention。
- Herdr 0.8.2 不提供 turn identifier。`prompt_accepted` 或 sequence 已变化但 outcome 尚未
  durably settled 时，recovery 进入 attention，不按 sequence window 猜测或采用 live turn。
- recovery 用 `agent get` 验证 ownership。只有 durable `settled`/`receipt_observed` 的 terminal
  state 与 exact sequence 仍匹配时，才可继续 outcome。随后最多读取 80 行 detection output，
  只用于现有 fatal signal 分类；完整 output 不进入 SQLite、receipt 或日志摘要。
- durable `receipt_observed` 的 `task_verified=true` 在成功和 settled fatal recovery 中都保留；
  只有 verification 缺失或不为 true 才返回 `task_receipt_recovery_unverified`。
- stale phase 和 outcome 会保留为 `is_stale=1` receipt。status、resume 和 GC 不读取 stale
  receipt 作为当前 identity 或 pane ownership。
- settled output 命中登录墙、device login、provider retry exhaustion 或 invalid model 时，
  不会记成功，并保留稳定错误码与有界摘要。
- 声明的 output/file receipt 缺失返回 `task_receipt_missing`，即使 agent 已 idle/done。
  output-prefix 必须来自当前 turn 的新增输出，独立 prompt echo 返回
  `task_receipt_ambiguous`；未改变的既有 file receipt 返回 `task_receipt_stale`。

## 诊断顺序

先看 durable queue：

```bash
just status
```

`doctor` 不是纯静态诊断。对于环境与 CLI 均可用的 harness，它会启动或复用 agent，并提交
带 output receipt 的真实只读 readiness turn。用 repeatable filter 收窄单一 harness；
JSON 包含 compact summary、readiness 总耗时和 provision/turn/receipt phase timings：

```bash
just doctor --harness droid
just doctor --harness droid --harness codex
```

需要可比较的 current-build evidence 时，在 Herdr-managed pane 运行 structured matrix。不要从普通
shell 或 pull-request CI 运行真实 matrix：

```bash
just readiness-matrix --harness droid
just readiness-matrix --harness droid --harness codex
```

Matrix 中只有 `ready` row 是 `VERIFIED`。`attempt_count=0` 表示本机环境、executable 或 profile 在
probe 前不可用；`readiness_ci_forbidden` 表示命令检测到 CI 环境并拒绝 live probe。失败、过期和
不可解析结果均为 `NOT VERIFIED`。Raw prompt、terminal output、完整 response 和 provider error
summary 不会进入 matrix。

再读取结构化 agent 状态：

```bash
herdr agent get <agent-name>
herdr agent explain <agent-name> --json
```

只在需要确认是否出现真实 turn 时读取有限终端内容：

```bash
herdr agent read <agent-name> --source detection --lines 120
herdr agent read <agent-name> --source recent-unwrapped --lines 120
```

最后确认 integration：

```bash
herdr integration status
```

诊断时只引用携带信号的行，不复制完整 transcript，不把 runtime output 提交进 Git。

## 常见错误码

| 错误码 | 含义 | Queue 行为 |
| --- | --- | --- |
| `agent_not_ready` | startup 后未在有界时间内达到 interactive ready | 按 attempt 重试 |
| `agent_turn_not_observed` | prompt 或 atomic response 后 sequence 未前进 | 按 attempt 重试 |
| `agent_prompt_stalled` | Herdr 在 acceptance 窗口内未观察到状态变化 | transport 内有界重发 Enter |
| `herdr_timeout` | turn 未在 deadline 内 settled，或控制命令超时 | prompt 接受前按 attempt 重试；接受后进入 attention |
| `agent_blocked` | settle 后仍有真实交互阻塞 | 普通 queue terminal blocked |
| `agent_auth_failed` | settled output 命中明确认证失败信号 | 按 attempt 失败处理 |
| `agent_auth_required` | settled output 命中登录或 device-code 等待 | 按 attempt 失败处理 |
| `agent_model_invalid` | provider 拒绝默认 model identifier | 按 attempt 失败处理 |
| `agent_provider_failed` | provider 请求重试耗尽 | 按 attempt 失败处理 |
| `prompt_acceptance_timeout` | prompt command 超时且复查未观察到新 sequence | 按 attempt 重试；摘要含 phase/state/sequence |
| `task_receipt_missing` | 声明的输出前缀或非空文件不存在 | 按 attempt 失败处理 |
| `task_receipt_ambiguous` | output-prefix 与 prompt 独立行重合，无法证明 authorship | 按 attempt 失败处理 |
| `task_receipt_stale` | file receipt 在当前 turn 前后未改变 | 按 attempt 失败处理 |
| `lease_expired_unaccepted` | reconciliation 证明原 operation 未接受输入 | abandon 原 operation；dispatch 可按 budget 创建 replacement |
| `unsafe_turn_adoption` | runtime identity 或 sequence 不能证明同一 accepted turn | terminal attention；不发送 replacement prompt |
| `task_receipt_recovery_unverified` | settled recovery 的 durable verification 缺失或不为 true | terminal attention；不重复执行任务 |
| `completion_recovery_unverified` | structured-v2 已 settled，但 typed evidence 未 durable commit | terminal attention；不重读旧 terminal output |
| `completion_job_mismatch` / `completion_attempt_mismatch` | envelope identity 不属于当前 claim | 按 completion verification failure 处理 |
| `completion_fencing_token_mismatch` | envelope 使用旧 attempt token | 按 completion verification failure 处理 |
| `completion_envelope_duplicate` | 当前 output window 有多条 envelope | 按 completion verification failure 处理 |

Herdr 0.8.2 没有 active-turn cancellation command。`unsafe_turn_adoption` 的 fencing 只保护
SQLite current projection。它不能停止 agent，也不能撤销外部副作用。

## 收口检查

修改 lifecycle 逻辑后至少运行：

```bash
PYTHONPATH=src uv run pytest tests/test_attempt_transport.py tests/test_attempt_crash_matrix.py
just check
```

需要真实验证时用单 harness、只读 smoke 收窄变量：

```bash
just smoke --harness grok
just smoke --harness claude
```
