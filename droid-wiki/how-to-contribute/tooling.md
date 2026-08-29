# 工具与质量门禁

`justfile` 是源码 checkout 的稳定命令入口，`uv.lock` 是 Python 工具链锁定真源，
`pyproject.toml` 保存格式、类型、测试和 coverage 配置。不要在个人脚本、文档和 CI 中复制
一套会漂移的门禁。模块组织约束见[模式与约定](patterns-and-conventions.md)。

## `uv` 与 `just`

```bash
uv sync --locked
just --list
just check
```

- `uv sync --locked` 只接受 `uv.lock` 中的解析结果；`pyproject.toml` 的生产 dependencies 为空，
  开发工具全部精确固定在 `dependency-groups.dev`。
- `uv run ...` 在锁定虚拟环境中执行 Python 工具，避免使用全局版本。
- `just` 封装正确的 `PYTHONPATH=src`、输出路径和参数转发，是本地与 CI 共用入口。
- `just check` 先重新同步锁定环境并运行 `compileall`，随后串联静态、测试、安全、构建、性能
  与摘要阶段。

## 常用命令与 artifact

| 命令 | 用途 | 主要输出 |
| --- | --- | --- |
| `just lint` | 静态质量、架构、文档与仓库策略 | 控制台失败项 |
| `just test` | 全部 pytest 与 JSON report | `.orchestrator/quality/tests.json` |
| `just test-coverage` | branch coverage，门槛 80% | `.orchestrator/quality/coverage.json` |
| `just test-stability` | 三次测试并比较 nodeid outcome | `.orchestrator/quality/stability.json` |
| `just security` | secret、源码与依赖扫描 | `.orchestrator/quality/bandit.json`、`.orchestrator/quality/pip-audit.json` |
| `just build-metrics` | `npm pack --dry-run --json` | `.orchestrator/quality/build.json` |
| `just profile-tests` | 标准库 `cProfile` 证据 | `.orchestrator/quality/tests.pstats` |
| `just quality-summary` | 汇总 coverage、stability、build、security | `.orchestrator/quality/summary.md` |
| `just docs-generate` | 确定性重建 CLI reference | `docs/generated/cli.md` |
| `just docs-check` | 校验关键文档链接与生成物 freshness | 控制台失败项 |
| `just check` | 完整合并门禁 | 上述全部质量证据 |

`.orchestrator/quality/` 是本地和 CI 的运行 artifact，不是源码真源，不能提交。

## `just lint` 的每一道门

| 工具或脚本 | 当前策略 | 失败时应修什么 |
| --- | --- | --- |
| Ruff | `src/`、`tests/`、`scripts/`；E/F/I/UP/B/SIM，Python 3.12 | 语法、导入、升级、bugbear 与简化问题 |
| Black | Python 3.12，100 字符行宽，check-only | 格式化源文件，不添加宽泛 ignore |
| mypy | `src/` strict，含 unreachable warning | 类型契约、可达性与缺失标注 |
| Pylint | `src/herdr_orchestrator/` 的命名与重复代码 | 命名约定或抽取真实重复逻辑 |
| Vulture | `src/`、`tests/`、`scripts/`，置信度至少 90 | 删除或证明 dead code |
| Xenon | absolute C、module B、average A | 拆分复杂控制流和深化模块 |
| import-linter | `.importlinter` 的叶子模块边界 | 修复依赖方向，不制造循环依赖 |
| deptry | `src/` 的依赖使用 | 缺失、未使用或不合适依赖声明 |
| `scripts/check_repository.py` | 文件大小、行数、技术债 marker | 拆分文件或使用带 issue/owner 的 marker |
| `scripts/check_feature_flags.py` | flag 声明、consumer、文档、环境示例与测试 | 补齐完整生命周期 |
| `scripts/check_docs.py` | 关键 Markdown 本地链接与已文档化 recipe | 修复链接或 `justfile` 命令 |
| `scripts/generate_reference.py --check` | CLI parser 与生成 reference 一致 | 运行 `just docs-generate` 并审阅 diff |

`.importlinter` 明确禁止 `src/herdr_orchestrator/model.py` 和
`src/herdr_orchestrator/protocol.py` 反向依赖 catalog、CLI、config、delivery、Herdr、runner、
store 等编排模块。leaf module 应保持可复用，不应通过豁免绕过边界。

## 仓库策略与文档门禁

`scripts/check_repository.py` 同时检查 tracked 和未忽略的 untracked 文件：

- 单文件默认不超过 512 KiB；
- Python source 不超过 1,500 行；
- `tests/` 中 Python 文件不超过 2,500 行；
- 其他受检文本不超过 2,000 行；
- 技术债 marker 使用 `TODO(#123 owner=name):` 这类带 issue 与 owner 的格式。

`src/herdr_orchestrator/dashboard/static/cytoscape.min.js` 是明确的 vendored 大小豁免，不应新增
豁免掩盖可拆分代码。

`scripts/check_feature_flags.py` 以 `src/herdr_orchestrator/feature_flags.py` 为声明真源，每个
flag 必须同时存在生产 consumer、`docs/feature-flags.md` 生命周期行、`.env.example` 变量和
测试引用；删除 flag 也要删除失效生命周期行。telemetry/exporter 还需同步
`docs/observability.md`。

`scripts/check_docs.py` 校验 `README.md`、`AGENTS.md`、`CONTRIBUTING.md` 的本地链接，并确认
其中引用的 `just` recipe 存在。`scripts/generate_reference.py` 从
`src/herdr_orchestrator/cli.py` 的 parser 确定性生成 `docs/generated/cli.md`：

```bash
just docs-generate
just docs-check
```

不要直接编辑生成 reference。

## Coverage、稳定性、构建与性能

- `just test-coverage` 对 `herdr_orchestrator` 开启 branch coverage，`pyproject.toml` 将 80%
  设为硬门槛并显示缺失分支。
- `scripts/test_stability.py` 连续运行 `tests/` 三次，比较每个 pytest nodeid；任一退出失败或
  outcome 不一致都会失败。
- `scripts/build_metrics.py` 执行 `npm pack --dry-run --json`，记录耗时、entry 数、压缩与解压
  大小，不发布包。
- `just profile-tests` 用标准库 `cProfile` 运行 `tests/test_protocol.py`，生成
  `.orchestrator/quality/tests.pstats`，用于本地 hot-path 调查，不增加生产依赖。
- `scripts/quality_summary.py` 读取 coverage、stability、build、Bandit 和 pip-audit JSON，生成
  人类可读摘要；摘要不能替代原始阶段 outcome。

测试的 fixture、Node.js 与真实 smoke 边界见[测试](testing.md)。

## 安全门禁

`just security` 包含：

1. `detect-secrets-hook` 使用 `.secrets.baseline`，扫描 Git 已跟踪和未忽略的未跟踪文件；
2. Bandit 扫描 `src/` 的 medium/high severity 问题，写入
   `.orchestrator/quality/bandit.json`；
3. `pip-audit --local` 检查锁定环境的已知漏洞，写入
   `.orchestrator/quality/pip-audit.json`。

不要把真实 secret 加入 baseline 来让检查通过。安全问题按 `SECURITY.md` 私密报告，不在公开
issue 贴 exploit、token、prompt 或 terminal output。安全 JSON 可能包含本机包元数据，也不提交。

## Git hooks

`.pre-commit-config.yaml` 定义：

- pre-commit：`just lint` 与 `.secrets.baseline` secret scan；
- pre-push：`just test-coverage`。

```bash
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type pre-push
```

hooks 提供快速反馈，但不能替代 `just check`，也不能借自动化格式化任务外文件。

## CI 与质量证据

`.github/workflows/ci.yml` 在每个 PR 和 `main` push 上执行：

1. GitHub-hosted Ubuntu，Python 3.12、Node.js 24、`uv` 0.12.5、`rust-just` 1.57.0；
2. `uv sync --locked` 与 `compileall`；
3. lint、branch coverage、三轮 stability、security、npm build metrics、profiling；
4. 无论阶段成功与否都运行 `just quality-summary`，上传名为 `quality-<run-id>` 的 artifact，
   内容来自 `.orchestrator/quality/`，保留 14 天；
5. 最后逐项要求 lint、coverage、stability、security、build、profiling outcome 为 success。

PR 的 `pr-review` job 下载 artifact，并更新自动质量评论；artifact 或 summary 存在不等于门禁
成功，必须查看最终 enforcement 与原始 JSON。`main` 只有测试通过才进入 npm release plan；
version gate 使用专属 self-hosted runner，实际 OIDC `npm publish` 留在 GitHub-hosted runner，
不得引入长期 npm token。贡献者不应在本地模拟真实 publish。

## 失败定位

| 失败位置 | 先看什么 |
| --- | --- |
| Ruff/Black/mypy/Pylint | 第一条根因；修源文件后重跑 `just lint` |
| Vulture/Xenon | dead code 或过度复杂控制流，不扩大豁免 |
| import-linter/deptry | 模块责任、依赖方向与 `pyproject.toml` 声明 |
| repository/feature/docs | 对应 `scripts/check_*.py` 输出和 canonical source |
| coverage | 未覆盖 branch 与拒绝路径，不补无断言调用 |
| stability | `.orchestrator/quality/stability.json` 的 unstable nodeid |
| security | Bandit/pip-audit 原始 JSON；敏感内容不得贴公开 issue |
| package build | `.orchestrator/quality/build.json` 与 `package.json` files 清单 |
| 真实 Herdr | 按[调试与运行态排障](debugging.md)的四层证据检查 |

## 相关页面

- [贡献指南](index.md)
- [开发工作流](development-workflow.md)
- [测试](testing.md)
- [调试与运行态排障](debugging.md)
- [模式与约定](patterns-and-conventions.md)
