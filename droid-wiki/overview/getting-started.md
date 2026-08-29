# 快速开始

本页覆盖源码开发、项目级安装和一次性手动 Manager 三条入口。源码开发需要 Python 3.12、Node.js 20+、Herdr 0.8.2+、`uv` 与 `just`；已发布的 npm 入口不要求 clone 本仓库。

## 从源码运行

```bash
git clone https://github.com/oldwinter/herdr-orchestrator.git
cd herdr-orchestrator
uv sync --locked
just doctor
just test
```

`justfile` 通过 `uv run` 使用锁定环境。`just doctor` 校验 Python、Herdr、Git、profile 和已选 harness 的真实 readiness turn；真实 dispatch 必须在 `HERDR_ENV=1` 的 Herdr pane 内运行。

## 启动普通 durable queue

```bash
just seed
just status
just run-once
just run-until-idle --drain-timeout-seconds 3600
```

- `seed` 按 `dedupe_key` 幂等写入示例任务。
- `run-once` 处理一个受 replica 限制的 wave。
- `run-until-idle` 重复 wave，直到当前 worker pool 排空、遇到 blocked 或达到 deadline。
- `status` 区分 agent lifecycle 和 `task_verified`。

需要内容级验收时，enqueue 时声明 receipt：

```bash
just enqueue pi inspect workflows/prompts/pi-config-check.md inspect-v3 \
  --placement pane \
  --receipt-prefix "TASK-OK inspect"
```

参数语义见[CLI 契约](../api/cli-contracts.md)，queue 行为见[Durable execution](../features/durable-execution.md)。

## 安装到另一个仓库

```bash
npx skills add oldwinter/herdr-orchestrator \
  --skill herdr-orchestrator --agent '*' -y

cd /path/to/target-repository
npx --yes herdr-orchestrator install --project .
npx --yes herdr-orchestrator doctor --project .
```

`bin/herdr-orchestrator.mjs` 只管理 manifest 记录的 `.herdr-orchestrator/`、可选 project Skill 和 `.orchestrator/.gitignore`。安装器不会下载 Python，不修改目标仓已跟踪的 `.gitignore`，也不会覆盖用户改过的托管文件。完整契约见[安装与分发](../systems/installation-and-distribution.md)。

## 启动手动 Manager

只需要临时观察和协调当前 Herdr session 时：

```bash
npx --yes herdr-manager
npx --yes herdr-manager claude
```

默认候选顺序是 Grok、Codex、Claude。该命令要求 `HERDR_ENV=1`，并在包内 `manager/` policy 目录启动选定 harness。它不会创建 durable queue。高频使用可运行：

```bash
just install-manager
herdr-manager
```

这会额外显式安装 Manager Light sidebar 投影。详情见[手动 Manager](../systems/manual-manager.md)和[Manager Light](../systems/manager-light.md)。

## 启动 Dashboard

```bash
just dashboard
just dashboard --port 9000 --poll-seconds 1
```

服务只允许绑定 loopback 地址。默认 URL 是 `http://127.0.0.1:8765`。Dashboard 不读取 prompt 或 terminal output，也没有写 API。见[Dashboard](../systems/dashboard.md)。

## 开发检查

迭代时运行最小测试，收口前运行：

```bash
just check
```

该命令执行锁定依赖同步、compile、lint、80% branch coverage、三轮稳定性测试、安全扫描、构建指标、profiling 和质量摘要。贡献流程见[开发工作流](../how-to-contribute/development-workflow.md)。

## 关键源文件

| 文件 | 用途 |
| --- | --- |
| `README.md` | 用户入口与常用命令 |
| `justfile` | 源码 checkout 的稳定命令 |
| `pyproject.toml` | Python 包与质量工具配置 |
| `package.json` | npm runtime/manager 命令分发 |
| `workflows/multi-harness.toml` | 默认 workflow |
| `docs/installation.md` | 项目级安装和 release 契约 |
