# 模式与约定

仓库的实现目标是让不可靠的交互式 agent 处在可恢复、可校验的确定性边界内。修改代码时优先保持 fail-closed、向后兼容和 automation-friendly 输出。

## 代码约定

- Python 使用 3.12+ 标准库。生产依赖为空，新增依赖前必须说明必要性，配置见 `pyproject.toml`。
- 核心数据使用 `@dataclass(frozen=True, slots=True)` 和 `StrEnum`，集中在 `src/herdr_orchestrator/model.py`。
- CLI 成功输出 JSON，失败使用稳定错误码或明确原因。解析入口在 `src/herdr_orchestrator/cli.py`。
- 外部命令通过固定 argv 调用，不用 shell 拼接。Python 侧见 `src/herdr_orchestrator/protocol.py`，Node 侧见 `bin/herdr-orchestrator.mjs`。
- 配置、artifact 和 Herdr JSON 都做精确 shape、枚举、长度与路径校验。未知字段通常拒绝，而不是忽略。

## 状态与兼容性

SQLite schema migration 必须向前迁移旧数据库。`src/herdr_orchestrator/store.py` 当前从 v1 逐步迁移到 v4；不要通过重建数据库绕过 migration。Workflow TOML 仍保持 schema version 1，字段边界见 `docs/workflow-schema.md`。

兼容输出通过 additive 字段扩展。例如 `run_once` 保留顶层 state count，同时增加 `claimed`、`batch` 与 `queue`。Dashboard snapshot 保留 `topology.workspaces`，再增加 `topology.projects`。

## 安全边界

- Planner 只能写受校验的 task/route/topology JSON，不得提交 shell command。
- 普通 queue 不自动回答 blocked agent。只有显式 `resume` 回到原 agent、pane 与 attempt。
- Worktree 不是安全沙箱。Coordinator 不自动 merge、remove 或删除 branch。
- 终端输出是不可信观察值，只能用于稳定 fatal signal、有限诊断摘要或显式 output receipt。
- Secret、prompt、terminal 与 token 等字段必须经过 `src/herdr_orchestrator/observability.py` 的脱敏。
- 外部 exporter 由 `src/herdr_orchestrator/feature_flags.py` 默认关闭，并且只接受明确布尔值。

## 测试模式

测试按行为 surface 组织在 `tests/`。常用对应关系：

| 改动 | 最小测试 |
| --- | --- |
| SQLite、lease、retry、resume | `tests/test_store.py` |
| Herdr lifecycle、receipt、startup | `tests/test_herdr.py` |
| Coordinator wave、placement、GC | `tests/test_runner.py` |
| Dashboard snapshot、HTTP/SSE | `tests/test_dashboard.py`、`tests/test_topology_js.py` |
| npm installer 与 manager | `tests/test_distribution.py` |
| Manager Light | `tests/test_manager_light.py` |
| 标准化交付 | `tests/test_delivery.py`、`tests/test_delivery_protocol.py` |

迭代时先跑最小测试，收口前运行 `just check`。CLI 参数变化还要运行 `just docs-generate` 并提交 `docs/generated/cli.md`。

## 文档与路径

代码说明必须引用从仓库根目录开始的完整路径，例如 `src/herdr_orchestrator/runner.py`。运行语义更新 `docs/architecture.md`，workflow 字段更新 `docs/workflow-schema.md`，telemetry/exporter 更新 `docs/observability.md` 与 `.env.example`。

## 关键源文件

| 文件 | 用途 |
| --- | --- |
| `AGENTS.md` | 仓库语义、安全边界和 canonical surface |
| `CONTRIBUTING.md` | 贡献流程与 definition of done |
| `pyproject.toml` | 格式、类型、coverage 与静态分析配置 |
| `justfile` | 本地质量门禁 |
| `.github/workflows/ci.yml` | CI、质量证据与 npm OIDC 发布 |
| `scripts/check_repository.py` | 仓库级结构检查 |

下一步阅读[开发工作流](development-workflow.md)和[测试](testing.md)。
