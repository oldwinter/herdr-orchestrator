# 贡献指南

本仓库是本地优先的多 harness 工作流控制面。贡献的目标不是让交互式 agent “看起来完成”，
而是让确定性 coordinator 能通过 schema、状态前置条件、lease、receipt 与稳定错误码证明行为。
开始前先阅读仓库根目录的 `AGENTS.md`、`CONTRIBUTING.md`、`SECURITY.md`，以及本 Wiki 的
[模式与约定](patterns-and-conventions.md)。

## 领取工作

1. 从已有 issue 或明确需求开始。缺陷应具备可复现的观察行为、期望行为、最小复现和版本；
   功能请求应具备用户问题、可观察行为、验收条件、风险与恢复方案。模板位于
   `.github/ISSUE_TEMPLATE/bug.yml` 和 `.github/ISSUE_TEMPLATE/feature.yml`。
2. 先确认没有重复 issue、重叠的进行中 PR 或不明确的验收条件，再在 issue 中说明准备处理的
   范围。安全漏洞不得领取为公开工作，必须按 `SECURITY.md` 使用 GitHub 私密漏洞报告。
3. 把工作收窄到一个可评审结果：列出将修改的 canonical source、行为测试、文档和生成物，
   明确不在范围内的生产操作、远端写入或清理动作。
4. 修改前运行 `git status --short` 和 `git diff`。区分任务文件与用户已有改动；目标重叠且无法
   安全合并时应停止并说明冲突，不得 reset、clean 或强制 checkout。

详细的分支与评审步骤见[开发工作流](development-workflow.md)。

## 快速开始

源码开发需要 Python 3.12+、Node.js 20+、`uv` 和 `just`：

```bash
uv sync --locked
just test
```

锁定安装避免本机与 CI 漂移。提交评审前必须运行完整合并门禁：

```bash
just check
```

`just check` 会同步锁定环境，随后执行编译、静态分析、分支覆盖率、三轮稳定性、安全扫描、
npm 包构建指标、性能证据和质量摘要。各阶段见[测试](testing.md)与
[工具与质量门禁](tooling.md)。

## 从领取到合并

```mermaid
flowchart LR
    A[领取并确认验收条件] --> B[保护现有工作区]
    B --> C[创建主题分支]
    C --> D[先写最小行为测试]
    D --> E[实现最小改动]
    E --> F[同步文档与生成物]
    F --> G[运行 just check]
    G --> H[提交 PR]
    H --> I[处理评审与 CI]
    I --> J[获准后合并]
```

- **实现阶段**：在最相关测试文件中先补或同时补 focused behavioral test；正常路径之外还要
  覆盖拒绝、timeout、stale、并发/ownership 与隐私边界。
- **文档阶段**：用户可见 CLI 变化运行 `just docs-generate`；workflow schema、delivery、
  Dashboard、telemetry/exporter 或 feature flag 变化同步各自真源文档。
- **PR 阶段**：使用 `.github/pull_request_template.md`，链接 issue，描述行为与动机，列出
  focused tests 和 `just check`，说明 migration、外部副作用、feature flag、风险及可执行回滚。
- **合并阶段**：等待 CI 各门禁成功并处理评审意见；质量摘要已生成不代表门禁成功。未经明确
  授权不要自行 push、merge、release 或 deploy。

## 完成定义

一项贡献只有同时满足以下条件才达到完成定义：

- [ ] 需求和验收条件明确，改动与 issue 范围一致。
- [ ] 未覆盖任务外的已修改或未跟踪文件，未提交 `.orchestrator/`、prompt、完整 terminal
      output、credential、token 或真实 `.env`。
- [ ] 行为测试覆盖成功与失败边界；稳定错误码、CLI 退出码、兼容性和 ownership 约束已验证。
- [ ] 已运行最小相关测试与 `just lint`，最终 `just check` 成功。
- [ ] 用户可见行为、CLI reference、feature flag、观测配置和架构文档已按改动同步。
- [ ] PR 链接 issue，写清验证、风险和具体回滚步骤；所有 CI outcome 成功且评审意见已处理。
- [ ] 合并、发布和部署由有权限的人按仓库流程执行，没有绕过 hooks 或安全门禁。

## 改动与测试索引

| 改动区域 | 主要真源 | 优先测试 |
| --- | --- | --- |
| CLI 与参数转发 | `src/herdr_orchestrator/cli.py`、`justfile` | `tests/test_cli.py`、`tests/test_protocol.py` |
| workflow 配置 | `src/herdr_orchestrator/config.py`、`workflows/multi-harness.toml` | `tests/test_config.py` |
| durable queue 与 migration | `src/herdr_orchestrator/store.py` | `tests/test_store.py` |
| 调度、重试、GC、resume | `src/herdr_orchestrator/runner.py` | `tests/test_runner.py` |
| Herdr 生命周期与 receipt | `src/herdr_orchestrator/herdr.py` | `tests/test_herdr.py` |
| Dashboard 只读投影 | `src/herdr_orchestrator/dashboard/` | `tests/test_dashboard.py`、`tests/test_topology_js.py` |
| npm 安装、manager 与发布约束 | `bin/herdr-orchestrator.mjs`、`packages/herdr-manager/`、`package.json` | `tests/test_distribution.py`、`tests/test_manager_light.py`、`tests/test_release.py` |
| 标准化交付 | `src/herdr_orchestrator/delivery.py` | `tests/test_delivery.py`、`tests/test_delivery_protocol.py` |

## 真实运行面的边界

`just test` 与 `just check` 通过 fake runner、临时目录和本地子进程覆盖大多数行为，不依赖
真实账号。`just doctor --harness <name>` 与 `just smoke --harness <name>` 会跨到真实 Herdr
运行面，需要 Herdr pane、主机上的 Herdr 0.8.2+ 和已认证的目标 harness CLI。两者不属于
`.github/workflows/ci.yml` 的常规门禁。

pane 存在、标题变化、最终 `done` 或 `agent_settled=true` 都不能证明内容正确；声明 receipt
的任务还必须达到 `task_verified=true`。证据顺序与稳定错误码见
[调试与运行态排障](debugging.md)。

## 相关页面

- [开发工作流](development-workflow.md)
- [测试](testing.md)
- [调试与运行态排障](debugging.md)
- [工具与质量门禁](tooling.md)
- [模式与约定](patterns-and-conventions.md)
