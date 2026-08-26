# 贡献指南
Active contributors: oldwinter, chendongdong

本仓库是本地优先的多 harness 工作流控制面。贡献的核心不是让 agent “看起来完成”，
而是让确定性 coordinator 能用 schema、状态前置条件和 receipt 证明任务结果。开始修改前，
先读 `AGENTS.md`、`CONTRIBUTING.md` 和[模式与约定](patterns-and-conventions.md)。

## 快速开始

源码开发需要 Python 3.12+、Node.js 20+、`uv` 和 `just`：

```bash
uv sync --locked
just test
```

安装使用锁文件，避免本机依赖与 CI 漂移。提交评审前必须运行完整合并门禁：

```bash
just check
```

`just check` 会重新执行 `uv sync --locked`，随后检查编译、静态质量、分支覆盖率、
三轮稳定性、安全扫描、npm 包构建指标和性能证据。具体命令见[测试](testing.md)和
[工具与质量门禁](tooling.md)。

## 一次贡献的标准路径

```mermaid
flowchart LR
    A[确认问题与安全边界] --> B[保护现有工作区改动]
    B --> C[添加最小行为测试]
    C --> D[实现最小改动]
    D --> E[运行最小相关测试]
    E --> F[同步文档与生成物]
    F --> G[运行 just check]
    G --> H[提交 PR：风险与回滚]
```

1. 用 `git status --short` 和 `git diff` 识别用户已有的已跟踪、未跟踪改动；不要覆盖、
   清理或顺手格式化任务范围外的文件。
2. 先定位行为契约及其测试。例如 queue/store 行为对应
   `src/herdr_orchestrator/store.py` 与 `tests/test_store.py`，Herdr 生命周期对应
   `src/herdr_orchestrator/herdr.py` 与 `tests/test_herdr.py`。
3. 先补或同时补 focused behavioral test，再做最小实现；模块边界和 fail-closed 规则见
   [模式与约定](patterns-and-conventions.md)。
4. 迭代时运行最小相关测试和 `just lint`，收口时运行 `just check`。不要用一次偶然通过
   替代稳定性、覆盖率或安全门禁。
5. 用户可见 CLI 变化必须用 `just docs-generate` 更新 `docs/generated/cli.md`；配置、
   feature flag、观测出口或运行语义变化也要同步相应文档。
6. PR 使用 `.github/pull_request_template.md`，链接 issue，写明行为动机、验证、风险和
   可执行的回滚路径。

详细步骤见[开发工作流](development-workflow.md)。

## 真实运行面不是普通单元测试

`just check` 和 `just test` 使用 fake runner、临时目录与本地进程验证大多数行为，不要求
真实 Herdr session。`just doctor` 与 `just smoke` 会跨到真实运行面：

- `just doctor --harness droid` 检查 Herdr 环境、可执行文件、登录/模型可用性和只读
  readiness turn，适合按单一 harness 收窄环境问题。
- `just smoke --harness droid` 会真实启动或安全复用 agent、提交只读任务、等待生命周期
  变化并核验 receipt。
- 两者需要在 Herdr pane 内运行、主机安装 Herdr 0.8.2+，并且目标 harness CLI 已认证；
  它们不在 `.github/workflows/ci.yml` 的常规 `just check` 门禁中。

不要为了让 smoke 通过而发送写操作，也不要把 pane 存在、标题变化或最终 `done` 当作任务
正确性的证明。真实运行排障见[调试与运行态排障](debugging.md)。

## 必须守住的边界

- 不提交 `.orchestrator/`、prompt、完整 terminal output、credential、真实 `.env` 值或
  其他机器运行态。
- 未经用户明确授权，不 push、merge、发布、发送消息、删除 worktree、修改权限或接触生产。
- 不执行会丢弃用户未提交改动的 checkout/reset/clean 操作；worktree 是 checkout 隔离，
  不是安全沙箱。
- planner 只能输出受校验的 task JSON，不能让其生成并直接执行 shell 命令。
- `blocked`、`unknown`、timeout 和仅有 `agent_settled=true` 都不是内容验收成功；声明
  receipt 的任务必须达到 `task_verified=true`。
- 安全漏洞按 `SECURITY.md` 私下报告，不在公开 issue 披露。

## 改动与验证索引

| 改动区域 | 主要真源 | 优先测试 |
| --- | --- | --- |
| CLI 与参数转发 | `src/herdr_orchestrator/cli.py`、`justfile` | `tests/test_cli.py` |
| workflow 配置 | `src/herdr_orchestrator/config.py`、`workflows/multi-harness.toml` | `tests/test_config.py` |
| durable queue 与 migration | `src/herdr_orchestrator/store.py` | `tests/test_store.py` |
| 调度、重试、GC、resume | `src/herdr_orchestrator/runner.py` | `tests/test_runner.py` |
| Herdr 生命周期与 receipt | `src/herdr_orchestrator/herdr.py` | `tests/test_herdr.py` |
| Dashboard 只读投影 | `src/herdr_orchestrator/dashboard/` | `tests/test_dashboard.py`、`tests/test_topology_js.py` |
| npm 安装与发布 | `bin/herdr-orchestrator.mjs`、`package.json` | `tests/test_distribution.py`、`tests/test_release.py` |
| 标准化交付 | `src/herdr_orchestrator/delivery.py` | `tests/test_delivery.py`、`tests/test_delivery_protocol.py` |

## 提交前清单

- [ ] 改动没有覆盖用户原有未提交文件，也没有包含 `.orchestrator/` 运行态。
- [ ] 最小相关测试覆盖正常、拒绝和 stale/concurrent 路径。
- [ ] 已运行 `just lint` 与最小相关测试。
- [ ] 用户可见行为、CLI、feature flag 和观测配置的文档/生成物已同步。
- [ ] 已运行 `just check`，并审阅 `.orchestrator/quality/` 中的失败证据。
- [ ] PR 已说明 issue、验证、风险和回滚；没有自行 push、merge 或发布。

## 继续阅读

- [开发工作流](development-workflow.md)
- [测试](testing.md)
- [调试与运行态排障](debugging.md)
- [工具与质量门禁](tooling.md)
- [模式与约定](patterns-and-conventions.md)
