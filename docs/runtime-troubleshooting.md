# Runtime troubleshooting and learned contracts

本文记录真实多 harness 演练暴露的运行契约、诊断步骤和已经固化的防回归规则。

## 四层证据

不要把以下状态混为一谈：

1. **Provisioned**：后台 tab、pane 和 harness 进程已经存在。
2. **Interactive ready**：`herdr agent get` 返回 `interactive_ready=true`，agent 可以接收输入。
3. **Turn observed**：提交 prompt 后 `state_change_seq` 前进，通常先进入 `working`。
4. **Settled**：同一 turn 最终稳定返回 `idle`、`done` 或 `blocked`。

只有第 3、4 层都成立，普通 queue 才能写成功 receipt。`done` 证明 lifecycle 完成，不证明
输出内容正确；需要内容证明的任务应写结构化 artifact、测试结果或 acceptance receipt。

## Topology 证据

`just status` 的 job 项会包含 `placement`、`execution_path` 和
`herdr_workspace_id`。用户可见标题与内部 agent identity 是两套字段：标题不带 hash，
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
- 已 blocked agent 会拒绝普通 `agent prompt`；代理回答必须使用受控 terminal input，
  然后等待新的 sequence，不能复用普通 prompt 路径。
- retry receipt 必须保留每个 attempt，不能用最终成功覆盖第一次 timeout。
- 后台 tab 使用 `--no-focus` 是正确行为；“当前 pane 没看到活动”不是运行证据。

## 当前防回归行为

- 新 agent 固定 settle 后，有界等待 `interactive_ready=true`。
- startup 瞬态 blocked 会复查；普通 turn 中的 persistent blocked 仍是 terminal。
- prompt acceptance 最多等待 5 秒，要求观察到新的 lifecycle sequence。
- stalled prompt 最多重发两次 Enter；仍无变化返回 `agent_turn_not_observed`。
- 进入 `working` 后继续使用 workflow 的 `agent_timeout_seconds` 等待 settled。
- principal proxy 使用 pane literal text 加 agent Enter 回答 blocked worker，再等待新的
  lifecycle sequence；普通 queue 不自动回答。
- `unknown`、timeout、未观察到 turn 和协议错误都不会记成功。

## 诊断顺序

先看 durable queue：

```bash
just status
```

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
| `agent_turn_not_observed` | prompt/Enter 后 sequence 未前进 | 按 attempt 重试 |
| `agent_prompt_stalled` | Herdr 在 acceptance 窗口内未观察到状态变化 | transport 内有界重发 Enter |
| `herdr_timeout` | 已观察到 turn，但未在执行 deadline 内 settled，或控制命令超时 | 按 attempt 重试，耗尽后 failed |
| `agent_blocked` | settle 后仍有真实交互阻塞 | 普通 queue terminal blocked |
| `agent_auth_failed` | settled output 命中明确认证失败信号 | 按 attempt 失败处理 |

## 收口检查

修改 lifecycle 逻辑后至少运行：

```bash
PYTHONPATH=src python3 -m unittest -v tests.test_herdr
just check
```

需要真实验证时用单 harness、只读 smoke 收窄变量：

```bash
just smoke --harness grok
just smoke --harness claude
```
