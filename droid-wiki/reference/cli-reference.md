# CLI
Active contributors: oldwinter, chendongdong

Python CLI 的参数真源是 `src/herdr_orchestrator/cli.py`。命令与参数清单由 `scripts/generate_reference.py` 生成到 [`docs/generated/cli.md`](https://github.com/oldwinter/herdr-orchestrator/blob/main/docs/generated/cli.md)；本页只给出导航、稳定语义和自动化约定，不复制全部 argparse 文本。

## 调用入口

源码 checkout 的直接入口与生成文档一致：

```bash
PYTHONPATH=src uv run python -m herdr_orchestrator status \
  --workflow workflows/multi-harness.toml
```

日常开发优先使用 `justfile` 中的 recipe，例如 `just doctor`、`just status`、`just run-once`。npm 安装面由 `bin/herdr-orchestrator.mjs` 处理项目 manifest 和 runtime 转发；手动管理会话则使用 `just manager [harness]` 或 `npx herdr-manager`，它不模拟 durable queue 的 lease、retry 或 receipt。

直接 Python 子命令都要求 `--workflow`。相对的 workflow、prompt、response 和 goal CLI 路径以进程当前目录为基准；TOML 内相对路径的规则见[配置](configuration.md)。

## 命令分组

完整 required/optional 参数请查机器生成的 `docs/generated/cli.md`。

| 分组 | Python 子命令 | 主要用途 |
| --- | --- | --- |
| 诊断与 catalog | `doctor`, `catalog`, `profile`, `smoke` | 检查系统和真实 harness readiness；读取 compact catalog 或选中 profile |
| 入队与观察 | `seed`, `enqueue`, `status` | 幂等 seed、单任务入队、查看安全的 queue 当前视图 |
| 调度与恢复 | `run`, `retry`, `resume` | 持续/单 wave/排空执行；增加 failed job 预算；人工恢复 blocked attempt |
| 清理与展示 | `gc`, `dashboard` | 预览或关闭本 workflow 创建的终态 agent；启动本机只读 Dashboard |
| Opt-in 交付 | `deliver` | 运行 Wayfinder、spec/ticket DAG、隔离实现、双轴 review 与有界 repair |

稳定 `justfile` 映射包括：

- `just catalog` / `just catalog-json`、`just profile <harness>`
- `just seed`、`just enqueue ...`、`just enqueue-auto ...`、`just status`
- `just run`、`just run-once`、`just run-until-idle`
- `just retry <job-id>`、`just resume <job-id> <response-file>`
- `just gc`、`just gc-failed`、`just dashboard`
- `just doctor`、`just smoke`、`just deliver <goal-file>`

`just test`、`just lint`、`just security`、`just docs-check` 和 `just check` 是开发/质量命令，不是 coordinator runtime 子命令。

## 关键语义

### Controller 与 worker 选择

`run`、`enqueue` 和 `deliver` 接受 controller/worker runtime override：

- `--controller-harness auto|<harness>`：显式 harness 覆盖 TOML；`auto` 强制按 `droid → grok → codex → claude → hermes → pi` 从可执行候选中选择。
- 可重复的 `--worker-harness <harness>`：整个列表替换 TOML worker candidate pool；必须无重复且已有对应 `[[workers]]`。
- `enqueue --harness auto` 或省略 harness 时，controller 只输出严格 worker JSON；显式 harness 跳过这次路由 turn，但不能越过有效 pool。

### Run 模式

`run` 有三种模式：

- 无 mode：持续轮询；Ctrl-C 正常停止。
- `--once`：执行一个受 `max_parallel` 和 replica slot 限制的 wave。
- `--until-idle`（别名 `--drain`）：多 wave 有界排空；blocked 或 timeout 不算成功。

Run 输出区分本次 `batch` 与完整 `queue` counts。Worker override 可能导致 `worker_pool_idle=true` 但 `queue_idle=false`，自动化调用方不能只看“没有 claim”。

### Receipt、retry 与 resume

`enqueue` 的 `--receipt-prefix` 与 `--receipt-file` 互斥。前者要求 dispatch 后新增输出中出现匹配前缀的行；后者要求 execution root 内非空、未越界且相对 dispatch 前有变化的文件。声明 receipt 后，`task_verified` 不是 `true` 就不能成功。

`retry` 只接受 `failed` job，并增加 attempt budget；它保留 job id、dedupe key 和已使用 attempts。`resume` 只接受 `blocked` job，复用最近 receipt 记录的 agent、pane、placement 和同一 attempt，不重发原 prompt。详见[任务与收据](../primitives/jobs-and-receipts.md)。

### GC、Dashboard 与 deliver

- `gc` 必须选择 succeeded 或 failed scope，默认 dry-run；只有 `--apply` 才关闭候选 pane，且不会处理 worktree、blocked、foreign 或仍在使用的 agent。
- `dashboard` 只接受 loopback host，提供只读 HTTP/SSE；数据契约见 [Dashboard API](../api/dashboard-http-sse.md)。
- `deliver` 本身是 opt-in gate；成功停在隔离 integration branch，不会自动 push、PR、merge、release 或 deploy。Secret/production principal-proxy 请求必须升级给用户。

## 输出与退出码

除 catalog/profile 的 text 格式和长运行停止提示外，runtime 结果面向 automation 使用 JSON；错误写 stderr。`status` 不输出 job prompt，Dashboard 也不输出环境变量或 terminal 内容。

| 退出码 | 含义 |
| ---: | --- |
| `0` | 成功，或长运行被 Ctrl-C 正常停止 |
| `1` | 命令完成，但健康/验收条件未满足，例如 doctor、smoke、drain 或 resume 未成功 |
| `2` | argparse、配置、catalog、store、transport、Git、tracker、artifact 或其他 validation/dispatch 错误 |
| `3` | `deliver` 遇到 secret/production protected-category escalation |

`blocked`、`unknown`、timeout 和单纯 agent settled 都不是 task success。更严格的 wire/automation 约定见 [CLI contracts](../api/cli-contracts.md)。

## 生成文档的维护

不要直接编辑 `docs/generated/cli.md`。修改 `src/herdr_orchestrator/cli.py` 的用户可见参数后运行：

```bash
just docs-generate
just docs-check
```

生成器和文档入口分别是 `scripts/generate_reference.py` 与 `docs/generated/cli.md`。贡献流程见[开发工作流](../how-to-contribute/development-workflow.md)。

## 关键源文件

| 完整路径 | 用途 |
| --- | --- |
| `src/herdr_orchestrator/cli.py` | Python parser、command handler 与退出码 |
| `docs/generated/cli.md` | 机器生成的命令/参数清单 |
| `scripts/generate_reference.py` | CLI reference 生成器 |
| `justfile` | 源码 checkout 的稳定 recipe |
| `bin/herdr-orchestrator.mjs` | npm 安装、manager 与 runtime 转发入口 |
| `packages/herdr-manager/bin/herdr-manager.mjs` | `npx herdr-manager` 薄转发 |
