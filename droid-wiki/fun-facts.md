# 代码库趣事
Active contributors: oldwinter, chendongdong

以下统计以仓库当前提交 `61400e5`（2026-08-26）为基准；文件大小与债务标记只统计 Git
跟踪的仓库内容，并明确排除第三方 vendored bundle。

## 1. 初始提交的 1,249 行代码仍在服役

仓库的首个提交是 `b4899a7`，日期为 **2026-08-23**，提交说明是
`feat: initialize durable Herdr orchestrator`。对当前 Python 源码做逐行 blame 后，仍有
**1,249 行**来自这个初始提交，分布在 10 个至今仍存在的模块中。其中保留最多的是：

- `src/herdr_orchestrator/herdr.py`：295 行；
- `src/herdr_orchestrator/store.py`：233 行；
- `src/herdr_orchestrator/config.py`：176 行。

也就是说，Herdr adapter、SQLite store 和配置解析不只是最早出现的模块，还是当前实现中
最明显的“原始地层”。

## 2. 名字有功能定义，没有起源故事

从 2026-08-23 的首个提交开始，根目录
`README.md` 就使用 `herdr-orchestrator`；当前
`pyproject.toml` 对它的定义是
“Durable multi-harness workflow orchestration over Herdr”。代码和提交历史说明了
**Herdr 是 terminal runtime、orchestrator 是确定性控制面**，但没有记录为何 Herdr 采用
这个拼写，也没有留下可信的命名轶事。因此这里不把字形联想当成项目历史；有记录的背景可继续看
[项目轶闻](lore.md)。

## 3. 最长的自有文件其实是 Herdr 测试

排除
`src/herdr_orchestrator/dashboard/static/cytoscape.min.js`
后，按物理行数计算，当前最长的仓库文件是
`tests/test_herdr.py`，共有 **2,393 行、
90,538 字节**。若改按字节数计算，排除该 bundle 后最大的是生成的锁文件
`uv.lock`，为 **259,798 字节、1,484 行**。

Cytoscape 文件本身有 **435,503 字节**，但它是随 Dashboard 一起 vendored 的第三方
minified bundle，不是本仓库作者维护的源码；压缩后只有 31 个换行，因此也不适合参加源码
行数比较。其第三方 MIT 许可单独保存在
`src/herdr_orchestrator/dashboard/static/cytoscape.LICENSE.txt`。

## 4. 债务标记为零，不等于没有债务管理

截至 2026-08-26，在排除 vendored Cytoscape 和策略脚本自身后，Git 跟踪的一方文本中
`TO&#68;O`、`FIX&#77;E`、`X&#88;X`、`HA&#67;K` 四类标记的数量都是 **0**。
原因不是项目禁止记录技术债，而是
`scripts/check_repository.py` 要求每个标记采用
“议题编号 + owner”的可追踪格式，否则检查直接失败。策略脚本自身被豁免，是因为它必须写出
这些标记的正则表达式和合规示例；Cytoscape 则作为第三方压缩包整体跳过文本策略。

因此“零标记”更准确的解释是：仓库不接受没有负责人和 issue 的随手便签。潜在改进项见
[清理机会](cleanup-opportunities.md)。

## 5. 零运行时包依赖，却有六路 harness 和四层拓扑

`pyproject.toml` 明确声明
`dependencies = []`，而 `package.json` 也没有
`dependencies`、`devDependencies`、`optionalDependencies` 或 `peerDependencies`：
Python 包和 npm wrapper 的**运行时包依赖均为零**。这不表示它没有外部运行要求——Python
3.12+、Node.js 20+、Git、Herdr 及所选 harness CLI 仍由宿主机提供。

与这个很小的依赖面形成反差的是，2026-08-24 加入的
`profiles/harnesses/*.toml` 一次定义了六种 harness：
`droid`、`grok`、`codex`、`pi`、`claude`、`hermes`。默认工作流
`workflows/multi-harness.toml` 恰好配置六个 worker，
并把 `max_parallel` 设为 6。随后项目在 2026-08-25 加入只读拓扑画布，并于 2026-08-26
把无 DOM 的图投影抽到
`src/herdr_orchestrator/dashboard/static/topology.js`，
展示 `project → worktree → tab → pane` 四层关系。

这些名词的精确定义见[术语表](overview/glossary.md)，更多统计见
[数字中的项目](by-the-numbers.md)。
