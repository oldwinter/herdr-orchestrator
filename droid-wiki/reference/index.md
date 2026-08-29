# 参考手册
Active contributors: oldwinter, chendongdong

本节集中记录 herdr-orchestrator 的稳定接口：Workflow TOML、Python 与 SQLite 数据模型、运行与开发依赖，以及命令行入口。字段和行为分别以 `docs/workflow-schema.md`、`src/herdr_orchestrator/config.py`、`src/herdr_orchestrator/model.py`、`src/herdr_orchestrator/store.py`、`src/herdr_orchestrator/delivery_protocol.py` 和机器生成的 `docs/generated/cli.md` 为准。

## 页面导航

| 页面 | 回答的问题 |
| --- | --- |
| [配置](configuration.md) | Workflow、worker、planner、placement、标准化交付、harness profile 和可选 exporter 如何配置？ |
| [数据模型](data-models.md) | 内存对象、durable queue、attempt receipt 与交付 artifact 如何关联？ |
| [依赖](dependencies.md) | 哪些是生产依赖、开发依赖、Node 包关系、外部 CLI 与 vendored 前端代码？ |
| [CLI](cli-reference.md) | 有哪些稳定命令组、调用入口、输出约定与退出码？ |

若要先理解这些名词的生命周期，从[领域原语](../primitives/index.md)开始；若要追踪完整控制流，阅读[系统架构](../overview/architecture.md)和[Coordinator 与 durable queue](../systems/coordinator-and-queue.md)。

## 稳定性边界

- `docs/workflow-schema.md` 描述 schema v1；`src/herdr_orchestrator/config.py` 执行范围、路径和跨字段校验。
- `src/herdr_orchestrator/store.py` 的 SQLite schema 当前为 v4，并保留 v1 到 v4 的顺序迁移。
- `docs/generated/cli.md` 由 `scripts/generate_reference.py` 从 parser 生成，不能手工修改。
- `src/herdr_orchestrator/model.py` 的 frozen dataclass 是进程内传递对象，不自动构成公共 JSON wire schema。
- `src/herdr_orchestrator/delivery_protocol.py` 的严格 JSON loader 只服务显式 `deliver` 流程，不属于普通 queue 的 SQLite schema。
- Dashboard 是 SQLite 与 Herdr 状态的只读投影；其 HTTP/SSE 契约见 [Dashboard API](../api/dashboard-http-sse.md)。

## 关键源文件

| 完整路径 | 用途 |
| --- | --- |
| `docs/workflow-schema.md` | Workflow TOML 的文档化 schema |
| `docs/generated/cli.md` | 机器生成的 Python CLI 命令与参数清单 |
| `src/herdr_orchestrator/config.py` | Workflow loader 与校验 |
| `src/herdr_orchestrator/catalog.py` | Harness profile loader 与 compact/full payload |
| `src/herdr_orchestrator/model.py` | 共享 enum 和 dataclass |
| `src/herdr_orchestrator/store.py` | SQLite schema、迁移和状态变更 |
| `src/herdr_orchestrator/delivery_protocol.py` | 标准化交付 artifact 的严格 loader |
| `pyproject.toml`、`uv.lock` | Python 构建、开发依赖与锁文件 |
| `package.json`、`package-lock.json` | 根 npm 包和 Node wrapper 元数据 |
| `packages/herdr-manager/package.json` | `herdr-manager` 薄入口包 |
