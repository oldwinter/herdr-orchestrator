# 开发工作流

本页覆盖工作领取、主题分支、实现、拉取请求（PR）、评审和合并。代码与状态约束先见
[模式与约定](patterns-and-conventions.md)，门禁含义见[工具与质量门禁](tooling.md)。

## 1. 领取并定义工作

从 `.github/ISSUE_TEMPLATE/bug.yml` 或 `.github/ISSUE_TEMPLATE/feature.yml` 的信息开始：

- 缺陷：确认观察行为、期望行为、最小复现、版本和稳定错误码；公开材料只能使用脱敏输出。
- 功能：确认用户结果、可观察验收条件、兼容性、外部副作用、隐私风险与恢复路径。
- 安全问题：停止公开讨论，按 `SECURITY.md` 使用 GitHub 私密漏洞报告。

领取前检查是否已有重叠 issue 或 PR，并在 issue 中说明准备处理的范围。把需求转成可验证清单，
提前写下 canonical source、最小测试、文档/生成物和明确不做的事项；验收条件仍有歧义时先澄清，
不要让实现猜测产品意图。

## 2. 准备可复现环境

| 组件 | 本地要求 | 用途 |
| --- | --- | --- |
| Python | 3.12+ | `src/herdr_orchestrator/` 与 `scripts/` |
| Node.js | 20+；CI 使用 24 | npm wrapper、Dashboard JavaScript 测试与打包 |
| `uv` | CI 固定 0.12.5 | 按 `uv.lock` 安装 Python 与开发工具 |
| `just` | 源码 checkout 必需 | 执行 `justfile` 中的稳定命令入口 |
| Herdr | 0.8.2+，仅真实运行检查 | `doctor`、`smoke` 和交互式 agent |

```bash
uv sync --locked
```

不要用非锁定安装掩盖依赖漂移。开发容器提供 Python 3.12 与 Node.js 24，并自动执行同一
锁定同步；它不会替代主机 Herdr 与 harness 登录态。仅测试可选 exporter 时才参考
`.env.example`，真实 `.env` 绝不能提交。

可选安装提前反馈 hooks：

```bash
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type pre-push
```

`.pre-commit-config.yaml` 让 pre-commit 运行 `just lint` 与 secret scan，让 pre-push 运行
`just test-coverage`；它们不能替代最终 `just check`。

## 3. 保护工作区并创建分支

先读取现状：

```bash
git status --short
git diff
```

确认基线符合团队流程且不会覆盖现有修改后，在本地创建描述性主题分支：

```bash
git switch -c <topic-branch>
```

分支生命周期中的边界：

- 不移动、删除或顺手格式化任务范围外的已修改/未跟踪文件。
- 不使用 `git reset --hard`、`git clean` 或强制 checkout 获得“干净”状态。
- 需要 checkout 隔离时可以创建独立 worktree，但 worktree 不是安全沙箱；不要自动 merge、
  remove checkout 或删除 branch。
- 不关闭非本任务创建的 pane/agent，不提交 `.orchestrator/`、原始 prompt、完整 terminal
  output、secret、token 或本机 `.env`。
- 本地分支创建不等于远端授权；未经明确要求，不 push、merge、release、deploy 或修改权限。

目标文件与用户已有 diff 重叠时，先理解并保留现有改动。无法安全合并就停止并报告具体冲突。

## 4. 找到真源和最小行为契约

按改动类型只读取必要范围：

- workflow 字段：`docs/workflow-schema.md`、`workflows/multi-harness.toml`、
  `src/herdr_orchestrator/config.py`；
- queue、lease、retry、receipt：`docs/architecture.md`、`src/herdr_orchestrator/store.py`、
  `src/herdr_orchestrator/runner.py`；
- Herdr startup/turn/settle：`docs/runtime-troubleshooting.md`、
  `src/herdr_orchestrator/herdr.py`；
- CLI：`src/herdr_orchestrator/cli.py`、`justfile`、`docs/generated/cli.md`；
- npm distribution：`bin/herdr-orchestrator.mjs`、`packages/herdr-manager/`、`package.json`；
- opt-in delivery：`docs/standardized-delivery.md`、`src/herdr_orchestrator/delivery.py`。

需要选择 harness 时先运行 `just catalog`，不要一次读取全部 `profiles/harnesses/*.md`；只有
选中后才运行 `just profile <harness>` 读取完整 profile。

## 5. 测试先行并保持改动最小

在最相关的 `tests/test_*.py` 中先添加或同时添加 focused behavioral test。至少考虑：

1. 正常状态转换和当前 turn receipt；
2. 非法 schema、未知字段、未知状态和越界路径的 fail-closed 行为；
3. stale attempt、lease、并发写、资源 ownership 与 idempotency；
4. 稳定错误码、结构化 CLI 输出和退出码；
5. prompt、terminal output、credential 与环境变量不泄露的负面断言。

例如：

```bash
PYTHONPATH=src uv run pytest tests/test_store.py -q
PYTHONPATH=src uv run pytest tests/test_herdr.py -q
just lint
```

不要扩大 ignore、放松类型、依赖固定 sleep，或把确定性规则转移给 prompt。测试框架、fixture
和 Node.js 契约见[测试](testing.md)。

## 6. 同步文档和生成物

文档属于行为契约：

- 用户可见 CLI 参数或子命令变化：运行 `just docs-generate`，不要手改
  `docs/generated/cli.md`。
- `justfile` recipe 或关键文档链接变化：运行 `just docs-check`；
  `scripts/check_docs.py` 检查 `README.md`、`AGENTS.md`、`CONTRIBUTING.md`。
- feature flag：同步 `src/herdr_orchestrator/feature_flags.py`、`docs/feature-flags.md`、
  `.env.example`、生产 consumer 和测试。
- telemetry/exporter：同步 `docs/observability.md`、`.env.example` 与 feature flag 生命周期。
- workflow schema、delivery、Dashboard：分别同步 `docs/workflow-schema.md`、
  `docs/standardized-delivery.md`、`docs/dashboard.md`。
- 开发规则：同时审查 `AGENTS.md`、`CONTRIBUTING.md`、`docs/development.md` 和本 Wiki。

## 7. 从最小验证升级到完成定义

推荐顺序：

```text
单个测试用例或文件
        ↓
just lint
        ↓
just test；需要时 just test-coverage
        ↓
just check
```

`just check` 会更新 `.orchestrator/quality/`。该目录是本地运行证据，不进入 Git。失败时检查
具体子命令、机器 JSON 与 summary，不要反复重跑来掩盖 flaky test。

只有改动涉及真实 lifecycle、harness 启动、认证/模型或 integration 时，才在合适主机上运行：

```bash
just doctor --harness <name>
just smoke --harness <name>
```

真实运行检查必须只读、按单一 harness 收窄，详见[调试与运行态排障](debugging.md)。

## 8. 提交 PR、处理评审并进入合并

提交前再次审阅 `git diff`，确保分支只包含本任务。PR 使用
`.github/pull_request_template.md`，完整填写：

- **摘要**：行为、动机和 `Fixes #N` 等 issue 关联；
- **验证**：focused tests、`just check`，以及用户可见文档/生成 reference；
- **风险与恢复**：migration、外部副作用、feature flag、兼容性风险和可执行回滚步骤。

`.github/workflows/ci.yml` 会在 PR 上执行锁定工具链和所有质量阶段，上传 quality artifact，
并更新自动质量评论。每个阶段先继续执行只是为了收集完整证据，最后仍要求 lint、coverage、
stability、security、build、profiling 全部成功。

评审修改应继续遵循“最小测试 → `just lint` → `just check`”，并在 PR 中回答变更后的风险。
进入合并前确认：验收条件满足、CI 全绿、评审意见已处理、文档/生成物已同步、回滚可执行。
合并与后续 release 由有权限的人按仓库流程执行；不要绕过 hooks、强推或在本地模拟真实 publish。

## 完成定义

- [ ] issue 的可观察验收条件全部有实现和测试证据。
- [ ] 分支范围干净，没有用户运行态、凭据或任务外改动。
- [ ] focused tests、`just lint` 与最终 `just check` 成功。
- [ ] CLI reference、架构、schema、观测、feature flag 和 Wiki 已按影响同步。
- [ ] PR 已写明验证、兼容性、风险和具体恢复步骤。
- [ ] CI 所有强制 outcome 成功，评审意见已处理，合并由获授权流程完成。

## 相关页面

- [贡献指南](index.md)
- [测试](testing.md)
- [调试与运行态排障](debugging.md)
- [工具与质量门禁](tooling.md)
- [模式与约定](patterns-and-conventions.md)
