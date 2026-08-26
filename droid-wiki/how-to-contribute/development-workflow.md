# 开发工作流
Active contributors: oldwinter, chendongdong

本页把一次贡献从环境准备带到可评审状态。设计与代码约束先见
[模式与约定](patterns-and-conventions.md)，命令的门禁含义见
[工具与质量门禁](tooling.md)。

## 1. 准备可复现环境

| 组件 | 本地要求 | 用途 |
| --- | --- | --- |
| Python | 3.12+ | `src/herdr_orchestrator/` 与质量脚本 |
| Node.js | 20+；CI 使用 24 | npm wrapper、Dashboard JS 测试与打包 |
| `uv` | CI 固定 0.12.5 | 按 `uv.lock` 安装 Python 与开发工具 |
| `just` | 源码 checkout 必需 | 仓库稳定命令入口 `justfile` |
| Herdr | 0.8.2+，仅真实运行检查 | `doctor`、`smoke` 和交互式 agent |

先安装锁定环境：

```bash
uv sync --locked
```

不要用一次非锁定安装“修复”依赖漂移。开发容器会自动执行同一命令，并提供 Python 3.12
与 Node.js 24；但容器不会替代主机上的 Herdr 和已登录 harness CLI。仅测试可选 exporter
时才从 `.env.example` 准备本地环境，真实 `.env` 绝不能提交。

可选安装 Git hooks：

```bash
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type pre-push
```

`.pre-commit-config.yaml` 让 pre-commit 执行快速静态门禁与 secret scan，让 pre-push
执行覆盖率测试。hooks 是提前反馈，不替代最终 `just check`。

## 2. 先保护用户工作区

修改前先查看：

```bash
git status --short
git diff
```

把当前任务的目标文件与用户已有改动区分开。贡献期间遵守：

- 不覆盖、删除、移动或格式化任务范围外的已修改/未跟踪文件。
- 不用 `git reset --hard`、`git clean` 或强制 checkout 来获得“干净”状态。
- 需要 checkout 隔离时可创建独立 worktree，但不要把 worktree 当安全沙箱；任务权限边界
  仍然有效。
- 不关闭、回收非本次运行创建的 pane/agent，也不删除保留的原生 worktree。
- 不把 `.orchestrator/`、完整终端输出、原始 prompt、secret 或本机 `.env` 放入 Git。
- 除非用户单独明确授权，不 push、merge、release、deploy 或修改远端状态。

如果目标文件与用户改动重叠，应先保留并理解现有 diff；无法安全合并时停止并报告冲突，
不要猜测用户意图。

## 3. 找到真源和行为契约

先按改动类型阅读最小范围：

- workflow 字段：`docs/workflow-schema.md`、`workflows/multi-harness.toml`、
  `src/herdr_orchestrator/config.py`；
- queue、lease、retry、receipt：`docs/architecture.md`、
  `src/herdr_orchestrator/store.py`、`src/herdr_orchestrator/runner.py`；
- Herdr startup/turn/settle：`docs/runtime-troubleshooting.md`、
  `src/herdr_orchestrator/herdr.py`；
- CLI：`src/herdr_orchestrator/cli.py`、`justfile`、`docs/generated/cli.md`；
- npm distribution：`bin/herdr-orchestrator.mjs`、`package.json`；
- opt-in delivery：`docs/standardized-delivery.md`、
  `src/herdr_orchestrator/delivery.py`。

不要一次加载所有 `profiles/harnesses/*.md`。先用 `just catalog` 看紧凑 catalog，只在某
harness 被选中时用 `just profile <harness>` 读取其完整上下文。

## 4. 测试先行，保持改动最小

在现有最相关的测试文件中添加 focused behavioral test，或为新边界创建清晰的测试。至少考虑：

1. 正常状态转换；
2. 非法 schema、未知状态或不允许路径的 fail-closed 行为；
3. stale attempt、lease、并发写或资源 ownership；
4. 稳定错误码与 CLI 退出码；
5. 不泄露 prompt、terminal output、credential 的负面断言。

例如修改 store：

```bash
PYTHONPATH=src uv run pytest tests/test_store.py -q
```

修改 Herdr adapter：

```bash
PYTHONPATH=src uv run pytest tests/test_herdr.py -q
```

然后运行静态门禁：

```bash
just lint
```

最小测试选择和各测试文件职责见[测试](testing.md)。不要为了通过检查而扩大豁免、
放松类型或把确定性规则转移到 prompt。

## 5. 同步文档和生成物

文档属于行为契约，不是收尾装饰：

- 用户可见 CLI 参数、子命令变化：修改实现后运行 `just docs-generate`，不要手改
  `docs/generated/cli.md`。
- `justfile` recipe 或关键文档本地链接变化：运行 `just docs-check`；
  `scripts/check_docs.py` 会验证 `README.md`、`AGENTS.md`、`CONTRIBUTING.md`。
- 新增或修改 feature flag：同步 `src/herdr_orchestrator/feature_flags.py`、
  `docs/feature-flags.md`、`.env.example`、生产 consumer 和测试。
- telemetry/exporter 变化：同步 `docs/observability.md`、`.env.example` 和 feature flag
  生命周期。
- workflow schema、delivery 阶段、Dashboard 行为分别同步
  `docs/workflow-schema.md`、`docs/standardized-delivery.md`、
  `docs/dashboard.md`。
- 改变开发规则时同时审查 `AGENTS.md`、`CONTRIBUTING.md`、`docs/development.md` 与相关
  Wiki 页面是否需要同步。

## 6. 从 focused tests 升级到合并门禁

推荐顺序：

```text
单个 test 或单个测试文件
        ↓
just lint
        ↓
just test（需要时 just test-coverage）
        ↓
just check
```

`just check` 是收口要求，会创建或更新 `.orchestrator/quality/` 下的本地证据。这里是运行
产物，不应提交。失败时先看具体子命令和对应 JSON/summary，不要反复重跑掩盖不稳定测试。

真实 Herdr 验证另有边界：只有 lifecycle、harness 启动、认证/模型或运行集成改动确实需要
时，才在合适主机上用单 harness 的 `just doctor --harness <name>` 和只读
`just smoke --harness <name>`。详见[调试与运行态排障](debugging.md)。

## 7. 准备评审

按 `.github/pull_request_template.md` 提供：

- 行为与动机，并用 `Fixes #N` 等方式链接 issue；
- focused tests 与 `just check` 的验证结果；
- migration、外部副作用、feature flag 和兼容性风险；
- 具体回滚步骤，而不是只写“revert”；
- 用户可见文档或生成 reference 的同步情况。

安全漏洞遵循 `SECURITY.md` 的私下流程。PR 只把改动准备到可评审状态；不要擅自 push、
merge、release 或 deploy。

## 导航

- [贡献指南](index.md)
- [测试](testing.md)
- [调试与运行态排障](debugging.md)
- [工具与质量门禁](tooling.md)
- [模式与约定](patterns-and-conventions.md)
