# 依赖
Active contributors: oldwinter, chendongdong

herdr-orchestrator 的控制面刻意使用 Python 标准库；Python 包与 npm 包都没有第三方
runtime package dependency。外部能力通过本机 CLI 和 Herdr terminal runtime 提供。

## Runtime package dependency：零

| 分发面 | 证据 | 结论 |
| --- | --- | --- |
| Python | `pyproject.toml` 的 `[project].dependencies = []` | 安装后的 Python runtime 不拉取第三方 Python 包 |
| Python lock | `uv.lock` 中 root package `herdr-orchestrator==0.1.2` 只有 dev dependency group | Lock 中的第三方包用于开发/质量门禁，不是 runtime dependency |
| npm | `package.json` 没有 `dependencies`/`devDependencies`；`package-lock.json` 只有 root package | npm wrapper 只使用 Node 内建模块 |

主要标准库能力包括 `argparse`、`dataclasses`、`enum`、`json`、`pathlib`、`sqlite3`、
`subprocess`、`tomllib`、`concurrent.futures` 与 `http.server`。SQLite 是 Python 标准库
模块，不需要独立数据库服务。

## 必需或按功能启用的系统/CLI 工具

| 工具 | 版本/条件 | 何时需要 |
| --- | --- | --- |
| Python | 3.12+ | 所有 Python runtime 命令 |
| Herdr | 0.8.2+；`HERDR_ENV=1`，并具有 pane/workspace id | Dispatch、readiness、smoke、Dashboard topology；应从 Herdr pane 内运行 |
| Git | 本机 `git` CLI | `doctor` 总会检查；worktree placement 与标准化交付依赖 Git repository |
| 至少一个 harness CLI | `droid`、`grok`、`codex`、`pi`、`claude`、`hermes` 之一，且已登录 | Controller/worker 的真实 turn；配置中启用的 harness 必须有 profile，运行还需要可执行文件和健康认证态 |
| Node.js | 20+ | 仅 npm 一键安装、升级、卸载与 `npx` runtime wrapper |
| GitHub CLI `gh` | 已认证 | 仅 `[standardized_delivery].tracker_backend="github"`；会创建、更新和关闭该次交付的 issue |
| `just` | 无固定版本声明 | 仅源码 checkout 的稳定 recipe 入口 |
| `uv` | 能消费当前 `uv.lock` | 仅源码 checkout 的同步、测试和质量命令 |

`doctor` 不只检查 executable：它还验证 Herdr 环境、`herdr --version`、Git、profile 文件，
并对所选 harness 做有界真实 readiness turn。若 GitHub tracker 已启用，还会检查 `gh`。

Harness agent 的最高自动化启动参数由 `src/herdr_orchestrator/herdr.py` 固定，不是可安装的
Python dependency，也不能由 planner 注入：

| Harness | 参数 |
| --- | --- |
| Droid | `--auto high` |
| Grok Build | `--always-approve --permission-mode bypassPermissions` |
| Codex | `--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust` |
| pi | `--approve` |
| Claude Code | `--dangerously-skip-permissions` |
| Hermes | `--yolo --accept-hooks` |

这些参数降低本地交互确认，不扩大任务对 push、merge、发布、权限或生产操作的授权。

## Build 与开发工具

Python build backend 是 `setuptools.build_meta`，构建依赖固定为 `setuptools==80.9.0`。
`pyproject.toml` 与 `uv.lock` 固定下列 dev group：

| 工具 | 版本 | 用途 |
| --- | --- | --- |
| `pytest`, `pytest-cov`, `pytest-json-report` | 9.1.1, 7.1.0, 1.5.0 | 测试、branch coverage、机器报告 |
| `coverage` | 7.15.4 | Coverage 数据 |
| `ruff`, `black` | 0.16.4, 26.5.1 | Lint 与格式检查 |
| `mypy`, `pylint` | 2.3.1, 4.0.7 | 严格类型与补充 lint |
| `bandit`, `pip-audit`, `detect-secrets` | 1.9.4, 2.10.1, 1.5.0 | 安全检查 |
| `deptry`, `import-linter` | 0.25.1, 2.3 | Dependency 与 import architecture |
| `radon`, `vulture`, `xenon` | 6.0.1, 2.16, 0.9.3 | 复杂度与 dead-code gate |
| `pre-commit` | 4.6.2 | 本地 hook orchestration |

`justfile` 的 `just check` 会先执行 `uv sync --locked`，再运行 compile、lint、coverage、
stability、security、metrics、profiling 与 quality summary。新增 runtime dependency
需要更新 `pyproject.toml`、`uv.lock` 和分发/安全契约，不能只在源码中 import。

## Vendored Cytoscape

Dashboard 的 topology 图使用 vendored **Cytoscape.js 3.34.1**：

- 文件：`src/herdr_orchestrator/dashboard/static/cytoscape.min.js`
- 许可：文件头保留 Cytoscape Consortium 的 MIT License
- 分发：由 `pyproject.toml` package data 和 `package.json` files 清单随 runtime 打包
- 加载：Dashboard 从 `/assets/cytoscape.min.js` 本地提供，不依赖 CDN、npm install 或网络

因此 Cytoscape 是仓库内静态 vendor asset，而不是 Python/npm runtime package dependency。
升级时必须保留许可头，并同步验证 `tests/test_dashboard.py` 和
`tests/test_topology_js.py`。
