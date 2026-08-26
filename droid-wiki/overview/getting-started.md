# 开始使用
Active contributors: oldwinter, chendongdong

源码开发需要 Python 3.12+、Herdr 0.8.2+、至少一个已登录的 harness CLI、`uv` 和 `just`。npm 一键安装还需要 Node.js 20+。真实 dispatch 必须从 Herdr pane 内运行。

## 从源码运行

```bash
uv sync --locked
just doctor
just test
```

`just doctor` 会检查 `HERDR_ENV`、pane/workspace ID、Herdr、Git、profile 和每个启用 harness 的真实只读 turn。只有 JSON 顶层 `ok=true` 且每个 readiness 状态为 `ready` 才表示可派发。

默认工作流是 `workflows/multi-harness.toml`。常用命令：

```bash
just catalog
just profile codex
just seed
just run-once
just run-until-idle --drain-timeout-seconds 3600
just status
```

完整命令分组见[CLI 命令参考](../reference/cli-reference.md)，工作流字段见[配置参考](../reference/configuration.md)。

## 安装到其他仓库

Agent Skill 与 runtime 是两个独立层：

```bash
npx skills add oldwinter/herdr-orchestrator \
  --skill herdr-orchestrator --agent '*' -y

cd /path/to/target-repository
npx --yes herdr-orchestrator install --project .
npx --yes herdr-orchestrator doctor --project .
```

`bin/herdr-orchestrator.mjs` 会检测本机 harness，写入项目相对 workflow、对应 profiles、ownership manifest 和 runtime ignore 文件。它不下载 Python，也不覆盖用户修改的托管文件。详细契约见[安装与分发](../systems/installation-and-distribution.md)。

## 入队一个任务

先把完整任务写进 UTF-8 文件。需要内容级验收时声明机器收据：

```bash
mkdir -p .orchestrator/requests
$EDITOR .orchestrator/requests/inspect-readme.md

PYTHONPATH=src uv run python -m herdr_orchestrator enqueue \
  --workflow workflows/multi-harness.toml \
  --harness pi \
  --placement pane \
  --title "Inspect README" \
  --prompt-file .orchestrator/requests/inspect-readme.md \
  --dedupe-key inspect-readme-v1 \
  --receipt-prefix "TASK-OK inspect-readme"
```

同一 workflow 内重复 `dedupe_key` 不会新增 job。`--harness auto` 会让受限 controller 从当前 worker catalog 选择 harness；`--placement auto` 会按显式覆盖、worker 默认、确定性读写规则和有界 topology JSON 依次决策。

## 运行、恢复与清理

```bash
just run-until-idle --drain-timeout-seconds 3600
just status

# failed job 追加一次 attempt budget
just retry 42 --extra-attempts 1

# blocked job 由人工审查后回答
just resume 43 approval.txt

# 先 dry-run，再按需应用
just gc
just gc --apply
```

`blocked` 不是成功，也不会被普通 queue 自动回答。GC 不删除 worktree、checkout 或 branch，且只关闭有创建收据和当前 pane 一致性证据的 agent pane。状态语义见[任务与收据](../primitives/jobs-and-receipts.md)。

## 启动 Dashboard

```bash
just dashboard
just dashboard --port 9000 --poll-seconds 1
```

默认地址为 `http://127.0.0.1:8765`。Dashboard 只读展示 queue、attention、Herdr topology 和 lifecycle，详见[本地 Dashboard](../systems/dashboard.md)。

## 开发验证

迭代时先运行最小相关测试，收口前运行：

```bash
just check
```

它包含格式、静态类型、import contracts、覆盖率、三次稳定性运行、安全扫描、包构建指标与文档新鲜度。贡献流程见[开发工作流](../how-to-contribute/development-workflow.md)和[测试](../how-to-contribute/testing.md)。

## 关键源文件

| 文件 | 作用 |
| --- | --- |
| `justfile` | 源码 checkout 的稳定命令入口 |
| `workflows/multi-harness.toml` | 默认工作流 |
| `src/herdr_orchestrator/cli.py` | Python CLI 命令定义 |
| `bin/herdr-orchestrator.mjs` | npm 安装与转发入口 |
| `docs/development.md` | 锁定工具链与质量门禁 |
