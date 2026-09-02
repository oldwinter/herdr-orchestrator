# 工具与质量门禁
Active contributors: oldwinter, chendongdong

`justfile` 是源码 checkout 的稳定命令入口，`uv.lock` 是 Python 工具链锁定真源，
`pyproject.toml` 保存工具配置。不要在文档、CI 和个人脚本中复制一套会漂移的门禁。
代码组织约束见[模式与约定](patterns-and-conventions.md)。

## 常用入口

| 命令 | 用途 | 主要输出 |
| --- | --- | --- |
| `uv sync --locked` | 安装锁定运行/开发环境 | 本地虚拟环境 |
| `just lint` | 静态质量、架构、文档和仓库策略 | 控制台失败项 |
| `just test` | 完整测试与 JSON report | 通过 run manifest 定位 |
| `just test-coverage` | branch coverage，门槛 80% | 通过 run manifest 定位 |
| `just test-stability` | 三次重复测试并比较 nodeid outcome | 通过 run manifest 定位 |
| `just security` | secret、源码与依赖安全扫描 | 通过 run manifest 定位 |
| `just build-metrics` | `npm pack --dry-run --json` | 通过 run manifest 定位 |
| `just profile-tests` | `cProfile` 基准证据 | 通过 run manifest 定位 |
| `just quality-summary` | 汇总 coverage/stability/build/security | run-scoped summary |
| `just docs-generate` | 重建 CLI reference | `docs/generated/cli.md` |
| `just docs-check` | 校验关键文档链接和生成物 freshness | 控制台失败项 |
| `just check` | 运行完整合并门禁 | 上述质量证据 |

`.orchestrator/quality/` 是本地/CI runtime artifact，不进入 Git。它适合定位失败和 PR 自动
摘要，不是源码真源。

## `just lint` 的组成

`just lint` 并不只是风格检查：

| 工具/脚本 | 当前策略 |
| --- | --- |
| Ruff | 对 `src/`、`tests/`、`scripts/` 启用 E/F/I/UP/B/SIM，目标 Python 3.12 |
| Black | 100 字符行宽，check-only |
| mypy | `src/` strict mode，包含 unreachable warning |
| Pylint | 检查命名与重复代码 |
| Vulture | dead code，最低置信度 90 |
| Xenon | 圈复杂度：absolute C、module B、average A |
| import-linter | 执行 `.importlinter` 中 model/protocol 叶子模块边界 |
| deptry | 检查 `src/` 中未使用、缺失或不合适依赖 |
| `scripts/check_repository.py` | 文件大小、行数和技术债 marker |
| `scripts/check_feature_flags.py` | feature flag 声明、consumer、文档、环境示例与测试 |
| `scripts/check_docs.py` | 关键 Markdown 本地链接和已文档化 `just` recipe |
| `scripts/generate_reference.py --check` | `docs/generated/cli.md` 与 parser 保持一致 |

格式问题应修改源文件，不要给工具增加宽泛 ignore。`.importlinter` 明确禁止
`src/herdr_orchestrator/model.py` 和 `src/herdr_orchestrator/protocol.py` 反向依赖
catalog、CLI、config、delivery、Herdr、runner 或 store 等编排模块。

## 仓库策略检查

`scripts/check_repository.py` 同时检查 tracked 与未忽略的 untracked 文件：

- 单文件默认不超过 512 KiB；
- Python source 不超过 1,500 行；
- `tests/` 中 Python 文件不超过 2,500 行；
- 其他受检文本不超过 2,000 行；
- 技术债标记必须使用 `TODO(#123 owner=name):` 这类带 issue 和 owner 的格式。

`src/herdr_orchestrator/dashboard/static/cytoscape.min.js` 是明确的 vendored 大小豁免。
不要新增豁免来掩盖可拆分的生产代码或测试。

## Feature flag 与文档门禁

`scripts/check_feature_flags.py` 以
`src/herdr_orchestrator/feature_flags.py` 的声明为基准，要求每个 flag 同时具备：

1. 生产 consumer；
2. `docs/feature-flags.md` 生命周期行；
3. `.env.example` 环境变量；
4. 测试引用。

删除 flag 时也要删除 lifecycle 中失去声明的行。telemetry/exporter 改动还必须同步
`docs/observability.md`。

`scripts/check_docs.py` 当前验证 `README.md`、`AGENTS.md`、`CONTRIBUTING.md` 的本地链接，
并确认其中出现的 `just` 命令确实存在于 `justfile`。CLI reference 由
`scripts/generate_reference.py` 从 `src/herdr_orchestrator/cli.py` 的 parser 确定性生成：

```bash
just docs-generate
just docs-check
```

不要直接编辑 `docs/generated/cli.md`。更广的文档同步清单见
[开发工作流](development-workflow.md)。

## 测试、稳定性与性能证据

`just test-coverage` 使用 branch coverage，并以 `pyproject.toml` 的 80% 为硬门槛。
`scripts/test_stability.py` 把 `tests/` 连续运行三次，比较每个 pytest nodeid 的 outcome；
任一次失败或结果不一致都使门禁失败。

`scripts/build_metrics.py` 执行 `npm pack --dry-run --json`，记录时长、条目数、压缩与解压
大小。`just profile-tests` 只用标准库 `cProfile` 对 `tests/test_protocol.py` 生成可调查的
基准证据，不引入 runtime dependency。`scripts/quality_summary.py` 汇总机器 JSON，不能
替代逐项检查原始失败。测试使用方法见[测试](testing.md)。

## 安全门禁

`just security` 包括：

- `detect-secrets-hook` 使用 `.secrets.baseline`，扫描 Git 已跟踪和未忽略的未跟踪文件；
- Bandit 扫描 `src/` 中 medium/high severity 问题；
- `pip-audit --local` 检查锁定环境中的已知依赖漏洞。

不要通过把真实 secret 加进 baseline 来让检查通过。安全漏洞应按 `SECURITY.md` 的私下
流程处理。扫描 JSON 位于 run-scoped `.orchestrator/quality/runs/<run-id>/`，可能包含本机包元数据；不要提交。

## Git hooks

`.pre-commit-config.yaml` 定义：

- pre-commit：`just lint` 与基于 `.secrets.baseline` 的 secret scan；
- pre-push：`just test-coverage`。

安装：

```bash
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type pre-push
```

hooks 提供提前反馈，但最终仍运行 `just check`。不要绕过 hooks 后把未经验证的改动交付，
也不要借 hooks 自动格式化任务范围外的用户文件。

## CI 如何执行同一门禁

`.github/workflows/ci.yml` 在 pull request 和 main push 上运行：

1. GitHub-hosted Ubuntu，Python 3.14.7、Node.js 26.8.1、`uv` 0.12.7、
   `rust-just` 1.57.0；
2. `uv sync --locked` 与 compileall；
3. lint、coverage、stability、security、build、profiling；
4. 无论前置结果如何生成 summary 并上传 `.orchestrator/quality/`，保留 14 天；
5. 最后逐项强制 outcome 为 success。

PR job 会更新自动质量评论。main 上只有测试通过后才进入 npm release plan；version gate
使用专属 self-hosted runner，但实际 OIDC `npm publish` 必须留在 GitHub-hosted runner，
不能引入长期 npm token。贡献者不应在本地模拟真实 publish，也不应因本地测试需要修改该
信任边界。

## 失败定位

| 失败位置 | 先看什么 |
| --- | --- |
| lint/format/type | 对应工具第一条根因；随后重跑 `just lint` |
| import boundary | `.importlinter` 与模块责任，不要循环依赖 |
| repository policy | 文件拆分、带 owner 的 debt marker |
| docs freshness | 运行 `just docs-generate`，审阅生成 diff |
| coverage | 缺失 branch 和负面路径，而非只补无断言调用 |
| stability | run manifest 中 stability artifact 的 unstable nodeid |
| security | Bandit/pip-audit 原始 JSON；secret 不得贴入 issue |
| package build | run manifest 中 build artifact 与 `package.json` files 清单 |
| 真实 Herdr | 按[调试与运行态排障](debugging.md)的证据层检查 |

## 导航

- [贡献指南](index.md)
- [开发工作流](development-workflow.md)
- [测试](testing.md)
- [调试与运行态排障](debugging.md)
- [模式与约定](patterns-and-conventions.md)
