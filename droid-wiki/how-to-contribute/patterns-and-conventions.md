# 模式与约定
Active contributors: oldwinter, chendongdong

本仓库用确定性控制面包住不可信模型输出。新增功能时优先保持 schema、状态前置条件、错误码和 receipt 证据清晰，而不是把策略转移到 prompt。

## 模块边界

`src/herdr_orchestrator/model.py` 保存跨模块不可变数据结构，`src/herdr_orchestrator/protocol.py` 保存子进程命令协议。`.importlinter` 禁止这两个叶子模块依赖 coordinator、store、delivery 或 CLI。新领域类型应先判断是否被三个以上系统共享，再决定是否放入 model。

业务流由较深模块组合：

- `src/herdr_orchestrator/runner.py` 组合 queue、router、topology、catalog、transport 和 observability；
- `src/herdr_orchestrator/delivery.py` 组合 protocol、Git worktree、tracker、controller 和 review；
- `src/herdr_orchestrator/dashboard/projector.py` 组合只读 observation，不反向修改 store 或 Herdr。

## Fail closed

外部输入使用 allowlist 和精确 shape：

- `src/herdr_orchestrator/config.py` 对枚举、长度、路径和 timeout 设边界；
- `src/herdr_orchestrator/planner.py` 只接受精确 JSON key；
- `src/herdr_orchestrator/delivery_protocol.py` 校验 artifact key、枚举、DAG 顺序、commit 和 acceptance；
- `src/herdr_orchestrator/protocol.py` 只运行 argv 列表，不使用模型生成的 shell 字符串。

不认识的状态、字段、harness、placement 或 schema 都应返回稳定错误，而不是猜测。

## 状态与错误

错误通过稳定字符串传播，例如 `agent_turn_not_observed`、`task_receipt_stale` 或 `job_lease_lost`。CLI 在 `src/herdr_orchestrator/cli.py` 中把预期错误映射为退出码 `2`，principal-proxy 敏感升级使用退出码 `3`。不要用异常文本替代可测试的错误码。

SQLite 写操作使用参数化 SQL。Claim、outcome、resume 和 placement 更新都带状态与 attempt 前置条件，见 `src/herdr_orchestrator/store.py`。新增状态转换时必须测试并发或 stale caller 路径。

## 路径和所有权

相对 runtime 路径必须解析到明确 root。Planner output 和 delivery artifact 必须位于 workspace 的 `.orchestrator` 下；installer 只管理 `bin/herdr-orchestrator.mjs` 允许的三类根，并拒绝 symlink 重定向。Worktree 只是 checkout 隔离，不是安全沙箱。

## 文档和命令

稳定入口写在 `justfile`。用户可见 CLI 变更后运行：

```bash
just docs-generate
just docs-check
```

`scripts/generate_reference.py` 生成 `docs/generated/cli.md`，不要手工修改生成文件。`scripts/check_docs.py` 会验证 README、AGENTS 和 CONTRIBUTING 中的本地链接及 `just` 命令。

## 技术债约定

`scripts/check_repository.py` 限制源码、测试和文本文件行数，并要求所有技术债标记都使用带 issue 与 owner 的格式：

```text
TODO(#123 owner=name):
```

Vendored `src/herdr_orchestrator/dashboard/static/cytoscape.min.js` 是明确的大小豁免。不要用新增豁免绕过可拆分的本仓库代码。

## 测试模式

- 配置、协议和 store 使用纯 Python unit tests；
- Herdr adapter 注入 fake command runner，断言完整 argv、超时和响应顺序；
- Dashboard projector 使用 fixture 验证白名单与 drift；
- `src/herdr_orchestrator/dashboard/static/topology.js` 保持无 DOM，由 `tests/test_topology_js.py` 用 Node 直接求值；
- npm wrapper 由 `tests/test_distribution.py` 在临时 Git 仓库中验证 ownership、symlink 和幂等行为。

详见[测试](testing.md)、[工具链](tooling.md)和[安全](../security.md)。

## 关键源文件

| 文件 | 作用 |
| --- | --- |
| `.importlinter` | 叶子模块依赖契约 |
| `scripts/check_repository.py` | 文件大小和技术债策略 |
| `scripts/check_feature_flags.py` | Feature flag 生命周期契约 |
| `scripts/check_docs.py` | 文档链接和命令契约 |
| `src/herdr_orchestrator/protocol.py` | 子进程 argv 和 JSON/text 协议 |
| `tests/test_herdr.py` | 生命周期契约的主要回归套件 |
