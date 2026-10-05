# Crash recovery 验收

[Issue #40](https://github.com/oldwinter/herdr-orchestrator/issues/40) 覆盖 queue ownership、
completion evidence、delivery recovery、installer recovery 和 quality publication。
本次以 `f48527c21700bc67a402dcdcfc646b8bf7d57bd8` 为基线核对规格，补齐剩余实现和恢复验证。

## 规格与代码对应

下表覆盖 issue 中全部 65 条 user story。测试入口用于复验行为，不表示真实 provider 已通过验收。

| Story | 行为契约 | 实现与测试入口 |
| --- | --- | --- |
| 1–13 | Attempt identity、CAS fencing、resume operation token、phase、恢复和 stale receipt | [attempts.py](../../src/herdr_orchestrator/attempts.py)、[attempt_runtime.py](../../src/herdr_orchestrator/attempt_runtime.py)、[attempt crash tests](../../tests/test_attempt_crash_matrix.py)、[store tests](../../tests/test_store.py) |
| 14–21 | Attempt-bound completion、历史兼容、显式 verification、幂等结果 | [completion.py](../../src/herdr_orchestrator/completion.py)、[completion transport tests](../../tests/test_completion_transport.py)、[completion store tests](../../tests/test_completion_store.py) |
| 22–30 | 六 harness matrix、过滤、稳定错误、版本身份、有界重试、隐私 | [readiness.py](../../src/herdr_orchestrator/readiness.py)、[readiness tests](../../tests/test_readiness.py) |
| 31–39 | Delivery owner lease、journal、tracker marker、Git/receipt/review reconciliation | [delivery journal](../../src/herdr_orchestrator/delivery_journal.py)、[delivery recovery](../../src/herdr_orchestrator/delivery_recovery.py)、[delivery tests](../../tests/test_delivery_journal.py) |
| 40–50 | Installer prior/desired inventory、原子替换、冲突保留、doctor、uninstall | [installer journal](../../bin/installer-journal.mjs)、[packed installer tests](../../tests/test_installer_journal.py) |
| 51–58 | 独立 quality run、manifest、digest、NOT VERIFIED、原子发布与独立 gate | [quality bundle](../../scripts/quality_bundle.py)、[quality storage](../../scripts/quality_storage.py)、[quality tests](../../tests/test_quality_bundle.py)、[CI](../../.github/workflows/ci.yml) |
| 59–60 | 分段验证、兼容 migration、稳定 CLI 错误 | [architecture](../architecture.md)、[store tests](../../tests/test_store.py)、[CLI reference](../generated/cli.md) |
| 61–64 | 模式隔离、Dashboard 只读、journal 隐私、无新增 runtime dependency | [AGENTS.md](../../AGENTS.md)、[dashboard tests](../../tests/test_dashboard.py)、[observability tests](../../tests/test_observability.py)、[package metadata](../../pyproject.toml) |
| 65 | 各运行面共享中断、重启和可观察结果比较 | [crash-matrix driver](../../tests/crash_matrix.py) |

## 本次补齐的任务

- [#63](https://github.com/oldwinter/herdr-orchestrator/issues/63) 负责共享测试驱动器，比较正常运行与中断恢复的持久状态。
- [#64](https://github.com/oldwinter/herdr-orchestrator/issues/64) 修复 quality publication 丢失 owner 的窗口，使用共享驱动器验证进程重启。
- [#66](https://github.com/oldwinter/herdr-orchestrator/issues/66) 在 delivery 更新派生 state 快照前，将阶段变化追加到 owner-fenced journal。

最终验收包含专项测试、全量 gate 和独立 Standards/Spec review。

## 自动化验收边界

恢复测试使用临时项目和测试拥有的 adapter。测试比较持久状态、外部副作用次数及安全的 attention 结果。
Queue 在无法证明 accepted turn 身份时进入 attention，不发送重复 prompt；不能为得到相同成功状态而放松这个契约。

运行 `just check` 执行全量仓库 gate，其中包含 packed installer interruption matrix。
Quality bundle 保存 commit、invocation、command outcome 和 artifact digest。人工摘要不能绕过独立 gate。
Runtime state、完整 prompt、终端 transcript 和原始 provider response 不进入此文档或 Git。

## 真实 readiness 单独验收

在操作者控制的 Herdr-managed pane 中运行 `just readiness-matrix`。它读取启用的 harness，输出带
commit、package version、workspace identity、stable error 和 phase timing 的机器证据。
重复 `--harness` 可缩小诊断范围。认证、模型配置及 provider 故障修复不在本规格内。

本次调用环境缺少 `HERDR_ENV`、`HERDR_PANE_ID` 和 `HERDR_WORKSPACE_ID`。普通 shell 的 readiness
preflight 返回 `not_in_herdr`，六项均为 `NOT VERIFIED`，未派发真实 harness turn。
单元测试、crash matrix 和 GitHub-hosted CI 不构成实时兼容性证明。只有当前构建的所选 harness
全部返回有效 readiness evidence，才可声明该次 matrix 为 VERIFIED。

## 复验本次修复

先运行新增驱动器、结构化幂等 completion、quality 发布恢复和 delivery 阶段恢复测试。

```bash
PYTHONPATH=src uv run pytest tests/test_crash_matrix.py tests/test_attempt_crash_matrix.py tests/test_completion_transport.py tests/test_quality_publication_recovery.py tests/test_quality_pytest_report.py tests/test_delivery_stage_journal.py -q
```

随后运行接入共享驱动器的完整 delivery boundary matrix 和 packed installer matrix。

```bash
PYTHONPATH=src uv run pytest tests/test_delivery_journal.py -k test_crash_matrix_converges_before_and_after_each_delivery_boundary -q
just test-installer-crash-matrix
```

最后运行 `just check`。这些命令保留原有领域测试，不通过删减 interruption boundary 或放松
verification 条件获得成功。Quality 测试也使用当前安装的 pytest 生成真实 JSON report，覆盖
`subtests passed` 与省略零值计数的格式，并拒绝失败、跳过和计数矛盾的证据。
