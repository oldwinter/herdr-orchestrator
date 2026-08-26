# 参考手册
Active contributors: oldwinter, chendongdong

本 lens 汇总 herdr-orchestrator 的稳定配置面、持久化模型、依赖边界与命令行契约。内容以当前仓库中的实现和测试为准；源码入口主要是 `src/herdr_orchestrator/config.py`、`src/herdr_orchestrator/model.py`、`src/herdr_orchestrator/store.py`、`src/herdr_orchestrator/cli.py` 与 `src/herdr_orchestrator/delivery_protocol.py`。

| 页面 | 内容 |
| --- | --- |
| [配置参考](configuration.md) | Workflow TOML、harness profile、默认值、范围、覆盖顺序与跨字段校验 |
| [数据模型](data-models.md) | enum/dataclass、SQLite schema、Dashboard snapshot 与标准化交付 artifact |
| [依赖](dependencies.md) | 零 Python/npm runtime package dependency、外部 CLI、开发工具与 vendored Cytoscape |
| [CLI 参考](cli-reference.md) | 稳定命令面、参数、JSON 输出和退出码 |

## 稳定性边界

- Workflow schema v1、CLI 参数和 SQLite migration 是兼容性边界。
- `src/herdr_orchestrator/model.py` 中的 dataclass 是实现模块之间的强类型传递对象，不等同于可直接序列化的公共 wire schema。
- Dashboard 是由 SQLite 与 Herdr 运行态生成的只读投影；不要把未版本化的嵌套运行态字段当成长期承诺。
- 标准化交付 artifact 由严格 loader 校验，但只在显式 `deliver` 流程中产生，不属于普通 durable queue 的 job/receipt 数据。
- 更细的 CLI 自动化约定见 [API / CLI contracts](../api/cli-contracts.md)。
