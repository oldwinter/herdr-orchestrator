# 依赖
Active contributors: oldwinter, chendongdong

核心 Python runtime 只用标准库，没有第三方生产依赖。开发工具、Node 分发包装、外部 CLI 和 Dashboard 内置的 Cytoscape.js 属于不同依赖层，升级时不能把它们混成一张 runtime package 清单。

## 依赖概览

| 层 | 直接依赖数 | 真源 | 结论 |
| --- | ---: | --- | --- |
| Python 生产 runtime | 0 | `pyproject.toml` 的 `[project].dependencies = []` | 安装核心包不拉取第三方 Python runtime 包 |
| Python build | 1 | `pyproject.toml` 的 `[build-system]` | `setuptools==80.9.0` 只用于构建 |
| Python 开发工具 | 17 | `pyproject.toml` 的 `[dependency-groups].dev` | 全部精确 pin；`uv.lock` 锁定其传递图 |
| 根 npm 包 | 0 | `package.json`、`package-lock.json` | Node wrapper 只用内建模块并携带 Python/runtime 文件 |
| `herdr-manager` npm 包 | 1 | `packages/herdr-manager/package.json` | 依赖 `herdr-orchestrator ^0.1.6` |
| Vendored 浏览器库 | 1 | `src/herdr_orchestrator/dashboard/static/cytoscape.min.js` | 仓库内静态 asset，不经 npm/CDN 获取 |

当前 `uv.lock` 有 73 个 package entry，其中一个是 editable root `herdr-orchestrator==0.1.6`；其余是 17 个直接开发工具的解析结果和传递依赖，不会改变生产依赖为零这一事实。

## Python 生产与构建

`pyproject.toml` 要求 Python 3.12+，入口点是：

```toml
[project.scripts]
herdr-orchestrator = "herdr_orchestrator.cli:main"
```

核心实现使用 `argparse`、`concurrent.futures`、`dataclasses`、`enum`、`http.server`、`json`、`pathlib`、`sqlite3`、`subprocess`、`tomllib` 和 `urllib.request` 等标准库模块。SQLite 通过 Python 标准库提供，不需要独立数据库服务；可选 Sentry、PostHog 和 webhook exporter 也没有引入 SDK。

Build backend 是 `setuptools.build_meta`，构建环境依赖 `setuptools==80.9.0`。这是 build-time dependency，不是 `[project].dependencies` 中的生产 runtime dependency。

## Python 开发依赖

`pyproject.toml` 和 `uv.lock` 精确固定以下 17 个直接开发工具：

| 类别 | 工具与版本 |
| --- | --- |
| 测试与覆盖率 | `pytest==9.1.1`、`pytest-cov==7.1.0`、`pytest-json-report==1.5.0`、`coverage==7.15.4` |
| 格式、lint 与类型 | `ruff==0.16.4`、`black==26.5.1`、`mypy==2.3.1`、`pylint==4.0.7` |
| 安全 | `bandit==1.9.4`、`pip-audit==2.10.1`、`detect-secrets==1.5.0` |
| 依赖与架构 | `deptry==0.25.1`、`import-linter==2.13` |
| 复杂度与 dead code | `radon==6.0.1`、`vulture==2.16`、`xenon==0.9.3` |
| 本地 hook | `pre-commit==4.6.2` |

源码 checkout 使用 `uv sync --locked` 还原这些工具。质量入口见[开发工具](../how-to-contribute/tooling.md)；新增 Python 生产依赖时必须同时更新 `pyproject.toml`、`uv.lock`、分发测试和依赖审计。

## Node 包与 runtime

### 根包 `herdr-orchestrator`

`package.json` 当前版本是 `0.1.6`，要求 Node.js 20+，暴露两个 bin 名：

- `herdr-orchestrator` → `bin/herdr-orchestrator.mjs`
- `herdr-manager` → `bin/herdr-orchestrator.mjs`

根包没有 `dependencies` 或 `devDependencies`；`package-lock.json` 也只有 root package。它把 `bin/`、`manager/`、`plugins/manager-light/`、`profiles/`、`skills/`、Python 源码、Dashboard 静态资源和一个 planner prompt 一并打包。Node 是安装、升级、卸载和 runtime 转发层，不替代 Python 3.12+、Herdr 或 harness CLI。

### 薄包 `herdr-manager`

`packages/herdr-manager/package.json` 当前版本是 `0.1.0`，要求 Node.js 20+，只暴露 `packages/herdr-manager/bin/herdr-manager.mjs`。它唯一的生产 package dependency 是 `herdr-orchestrator ^0.1.6`，用于把 `npx herdr-manager` 转发到 canonical manager 实现；它不是第二套 coordinator。

安装和 manager 模式的边界见[安装与分发](../systems/installation-and-distribution.md)。

## 外部 runtime 与命令

这些工具不在 Python/npm dependency graph 中，但对应功能运行时需要它们：

| 工具 | 条件 | 使用场景 |
| --- | --- | --- |
| Python | 3.12+ | 所有 Python runtime 命令 |
| Herdr | 仓库文档基线为 0.8.2+ | Agent pane、workspace、worktree、readiness、smoke、Dashboard topology |
| Git | 本机 CLI | Doctor 检查、worktree placement、标准化交付 |
| Harness CLI | 启用的 `droid`、`grok`、`codex`、`pi`、`claude`、`hermes` | Controller 与 worker 的真实 turn；还需要各自登录态 |
| Node.js | 20+ | npm/npx 安装包装与 manager 入口 |
| GitHub CLI `gh` | 已认证 | 仅标准化交付的 GitHub tracker backend |
| `just` | 仓库未 pin 版本 | 源码 checkout 的稳定 recipe |
| `uv` | 能消费当前 `uv.lock` | 开发环境同步、测试和质量门禁 |

这些 executable 的存在不等于健康。`doctor` 还检查 Herdr 环境、profile、Git 和有界 harness readiness。命令用法见 [CLI](cli-reference.md)。

## Vendored Cytoscape.js

Dashboard topology 使用 vendored Cytoscape.js **3.34.1**：

| 完整路径 | 用途 |
| --- | --- |
| `src/herdr_orchestrator/dashboard/static/cytoscape.min.js` | 压缩后的浏览器 runtime；文件内版本为 3.34.1 |
| `src/herdr_orchestrator/dashboard/static/cytoscape.LICENSE.txt` | Cytoscape Consortium 的 MIT License |
| `src/herdr_orchestrator/dashboard/static/index.html` | 从 `/assets/cytoscape.min.js` 本地加载 |
| `src/herdr_orchestrator/dashboard/server.py` | 只从 allowlist 提供该 asset |

`pyproject.toml` 的 package data 和 `package.json` 的 files 清单都会分发 `.js` 与 `.txt`。因此它不是 npm install 产生的 runtime dependency，也不依赖 CDN 或网络。升级时要保留许可文件，并验证 `tests/test_dashboard.py` 与 `tests/test_topology_js.py`。

## 关键源文件

| 完整路径 | 用途 |
| --- | --- |
| `pyproject.toml` | Python 版本、build backend、生产与开发依赖 |
| `uv.lock` | Python 开发工具的完整解析结果 |
| `package.json` | 根 npm 包、bin、engine 与分发清单 |
| `package-lock.json` | 根 npm 包锁文件 |
| `packages/herdr-manager/package.json` | Manager 薄包及其一个 package dependency |
| `.env.example` | 可选 exporter 的无 SDK 配置模板 |
| `src/herdr_orchestrator/dashboard/static/cytoscape.min.js` | Vendored Cytoscape.js |
| `src/herdr_orchestrator/dashboard/static/cytoscape.LICENSE.txt` | Vendored 许可 |
