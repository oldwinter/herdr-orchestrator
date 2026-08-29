# 有据可查的代码库趣事

以下四项均重新取自 **2026-08-29** 的当前工作树与 `origin/main` `7291093`。
文件比较沿用[数字中的代码库](by-the-numbers.md)的排除口径；没有 Git、blame、
策略代码或 package manifest 证据的轶闻不写入本页。

## 1. 首日代码仍留下 1,249 行“原始地层”

仓库首个提交是 **2026-08-23** 的 `b4899a7`
（`feat: initialize durable Herdr orchestrator`）。对当前
`src/herdr_orchestrator/**/*.py` 逐行执行 Git blame，仍有 **1,249 行**归因于该提交，
分布在最初创建且延续至今的 10 个模块中。保留最多的三个完整路径是：

| 完整仓库路径 | 仍归因于初始提交的行 |
| --- | ---: |
| `src/herdr_orchestrator/herdr.py` | 295 |
| `src/herdr_orchestrator/store.py` | 233 |
| `src/herdr_orchestrator/config.py` | 176 |

blame 归因不等于这些行从未被周边重构，但它能可靠说明 Herdr adapter、SQLite store
与配置解析确实是当前代码中最厚的首日地层。它们如何分工见
[协调器与队列](systems/coordinator-and-queue.md)。

## 2. 最长的一方文本不是实现，而是 Herdr 测试

应用统一排除规则后，当前最长、同时也是字节数最大的文本文件是
`tests/test_herdr.py`：**2,393 行、90,538 字节**。最长的一方非测试源文件
`src/herdr_orchestrator/herdr.py` 则是 **1,416 行、51,029 字节**。

这个结论刻意不让生成物参加比赛：vendored/minified 的
`src/herdr_orchestrator/dashboard/static/cytoscape.min.js` 有 435,503 字节，
`uv.lock` 有 259,798 字节。前者是第三方 bundle，后者是依赖锁定结果；按可维护源码
比较时都应单列。

## 3. 四类债务关键字为零，但检查器知道它们

在排除 Wiki、视频、锁文件、生成报告与 vendored bundle 后，扫描所有跟踪的一方文本，
并把策略脚本自身单列，to-do、fix-me、hack 与 xxx 四类实际债务标记均为 **0**。
唯一的规范关键字命中来自 `scripts/check_repository.py` 自身，共 9 次：它必须在正则和
错误示例里写出这些词。

零标记不是“无需管理债务”的证明。`scripts/check_repository.py` 明确要求标记采用
“关键字后附 issue 编号与 owner”格式；不合规的随手便签会让检查失败。
当前可见的改进事项集中记录在[清理机会](cleanup-opportunities.md)。

## 4. 核心运行时无包依赖，最薄入口反而只有一个依赖

`pyproject.toml` 的 `dependencies = []`，说明 Python 包没有运行时 PyPI 依赖；
根 `package.json` 也没有 npm dependency 字段。开发环境并非零工具：
`pyproject.toml` 的 dev dependency group 明确锁定了 17 个测试、类型、质量和安全工具。

**2026-08-28** 新增的薄命令包则呈现另一种极简：
`packages/herdr-manager/package.json` 只有一个运行时依赖，
`herdr-orchestrator` `^0.1.6`；它的
`packages/herdr-manager/bin/herdr-manager.mjs` 只用固定 argv 转发，不通过 shell
拼接命令。包依赖少也不等于机器要求少：当前安装契约仍需要 Python 3.12+、Node.js 20+、
Git、Herdr 与至少一个可用 harness CLI。完整边界见[依赖参考](reference/dependencies.md)。
