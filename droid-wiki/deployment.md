# 部署、发布与维护
Active contributors: oldwinter, chendongdong

本项目的“发布”是把 `herdr-orchestrator` 发布到 npm，并为同一提交创建 GitHub
Release；它不等于把控制面部署到任何生产环境。使用者仍需在目标 Git 仓库中显式安装、
诊断并启动项目本地运行面。

## 发布与运行面的边界

发布链路的真源是：

- `package.json`：npm 包名、版本、公开访问级别、Node.js 版本要求、可执行文件与打包清单。
- `pyproject.toml`：Python 包版本、Python 3.12+ 要求、零运行时依赖及开发工具锁定。
- `bin/herdr-orchestrator.mjs`：npm 可执行文件以及安装、升级、诊断、卸载和运行时转发。
- `scripts/npm-release-plan.mjs`：查询 npm registry 并决定当前版本是否需要发布。
- `.github/workflows/ci.yml`：测试 gate、release plan、OIDC 发布和 GitHub Release。

`package.json` 声明 Node.js 20+，npm 包本身没有运行时 npm 依赖。目标机器还必须提供
Python 3.12+、Herdr，以及至少一个受支持的 harness CLI；Python 不会被复制或下载。
支持的 harness 名称为 `droid`、`grok`、`codex`、`pi`、`claude` 和 `hermes`。

## 从 npm 安装

在目标 Git 仓库根目录执行：

```bash
npx --yes herdr-orchestrator install --project .
```

为了可复现安装，可以固定一个已经发布的不可变版本：

```bash
npx --yes herdr-orchestrator@0.1.2 install --project .
```

安装器会自动探测本机 harness；也可以显式收窄：

```bash
npx --yes herdr-orchestrator install --project . \
  --harness droid \
  --harness codex
```

如果目标仓库已经有 `.agents/skills/`，安装器默认不注入项目 Skill。确需由安装器管理时
必须显式执行：

```bash
npx --yes herdr-orchestrator install --project . --install-skill
```

只安装可移植 Agent Skill、而不安装 Python 控制面和 workflow 时，使用独立分发入口：

```bash
npx skills add oldwinter/herdr-orchestrator \
  --skill herdr-orchestrator --agent '*' -y
```

## 项目本地受管面

`bin/herdr-orchestrator.mjs` 把以下内容安装到目标仓库：

| 目标路径 | 用途与 ownership |
| --- | --- |
| `.herdr-orchestrator/manifest.json` | 记录 schema、包版本、harness、Skill 偏好，以及受管文件的 SHA-256 |
| `.herdr-orchestrator/workflows/` | 项目相对 workflow 和 planner prompt |
| `.herdr-orchestrator/profiles/` | 仅包含已选择 harness 的紧凑与完整 profile |
| `.agents/skills/herdr-orchestrator/` | 可选的项目 Agent Skill |
| `.orchestrator/.gitignore` | 让 durable runtime state 留在本地 |

安装器还在目标仓库的 Git-local `.git/info/exclude` 中维护带起止标记的区块，而不修改受
版本控制的 `.gitignore`。只有 manifest 真正拥有的 Skill 才会被加入该区块。受管路径或
Git exclude 路径中的符号链接会被拒绝，以避免越界写入。

ownership 以 manifest 中的内容哈希为准：

- 未受管但内容冲突的文件会让安装在写入前停止。
- 内容相同但由其他工具安装的 Skill 可复用，但不会被安装器接管。
- 用户修改过的受管文件在重装、升级和卸载时都会保留。
- 部分协调返回退出码 `1`，并在 JSON 的 `preserved` 中列出需要人工处理的路径。
- `uninstall` 只删除哈希未变化的受管文件；处理后会移除 manifest，保留文件不再受管。

安装后先运行：

```bash
npx --yes herdr-orchestrator doctor --project .
```

只有输出中的 `installation.ok`、`runtime.ok` 和顶层 `ok` 均为 `true` 才算健康。manifest
版本与当前 npm wrapper 版本不一致会报告 `version_skew`。

## 版本同步

当前版本同时出现在 npm 与 Python 分发元数据中。发布变更必须保持以下位置一致：

1. `package.json` 的 `version`；
2. `package-lock.json` 中的包版本；
3. `pyproject.toml` 的 `project.version`；
4. `src/herdr_orchestrator/__init__.py` 中公开的 `__version__`。

`tests/test_distribution.py` 会核验 npm CLI 的 `--version`、`pyproject.toml` 与 Python
包版本一致；`tests/test_release.py` 会核验 release-plan 和 CI 发布约束。常规升级流程
可以从下面开始：

```bash
npm version patch --no-git-tag-version
# 同步 pyproject.toml 与 src/herdr_orchestrator/__init__.py
npm run release:plan
npm pack --dry-run --json
just check
```

需要破坏性或新增功能版本时，将 `patch` 换成合适的 `minor` 或 `major`。版本变更通过
正常 pull request 合入 `main`；不要在本地提前创建同名发布 tag。

## CI 测试 gate

`.github/workflows/ci.yml` 在 pull request 和推送到 `main` 时运行。当前测试 job 使用
GitHub-hosted `ubuntu-latest`，因此不可信 pull request 代码不会进入专用 self-hosted
runner。checkout 不持久化凭据，主要工具和 GitHub Actions 均固定版本或 commit SHA。

测试顺序为：

1. 安装 Python 3.14.7、Node.js 26.8.1、uv 0.12.7 和 rust-just 1.57.0；
2. 用 `uv sync --locked` 安装锁定工具链；
3. 编译 `src/`、`tests/` 和 `scripts/`；
4. 分别执行 `just lint`、`just test-coverage`、`just test-stability`、
   `just security`、`just build-metrics` 和 `just profile-tests`；
5. 始终生成质量摘要并上传 `.orchestrator/quality/`，保留 14 天；
6. 最后的 `Enforce all outcomes` 要求六个 gate 全部成功。

各质量步骤使用 `continue-on-error` 是为了收集完整证据，并不把失败变成成功。最后的
enforcement 失败会阻止 `release-plan`，进而阻止 npm 发布和 GitHub Release。

## Registry release-plan

只有通过测试的 `main` push 才运行 `release-plan`。该 job 使用标签
`[self-hosted, Linux, X64, herdr-orchestrator]`，执行：

```bash
node scripts/npm-release-plan.mjs --package-json package.json
```

脚本严格验证包名和 SemVer，再执行 `npm view herdr-orchestrator versions --json`：

- registry 已有该版本：输出 `publish=false`、`reason=version_exists`，本次是成功 no-op；
- registry 缺少该版本：输出 `publish=true`、`reason=version_missing`；
- 查询失败或返回结构无效：退出码 `2`，停止发布，绝不把网络失败猜成“新版本”。

因此，只有 `package.json` 中尚未出现于公开 registry 的确切版本才会进入发布 job。

## GitHub-hosted OIDC Trusted Publishing

npm Trusted Publishing 不支持 self-hosted runner。真正的 `publish` job 必须保留在
GitHub-hosted `ubuntu-latest`，并绑定 GitHub Environment `npm`。仓库侧与 npm 侧的一次
性配置必须精确匹配仓库、workflow 文件和 Environment：

```bash
npm trust github herdr-orchestrator \
  --file ci.yml \
  --repo oldwinter/herdr-orchestrator \
  --env npm \
  --allow-publish \
  -y
```

发布 job 只在 release-plan 输出 `publish=true` 时启动，并授予
`contents: write`（创建 GitHub Release）和 `id-token: write`（OIDC）权限。它会：

1. 以 `persist-credentials: false` checkout 对应的 `main` 提交；
2. 配置 Node.js 26.8.1 和 `https://registry.npmjs.org`；
3. 安装支持 OIDC 的 npm 12.0.2；
4. 执行 `npm ci --ignore-scripts`；
5. 执行 `npm publish --access public`。

不要添加 `NODE_AUTH_TOKEN` 或长期 npm token，也不要把该 job 移到 self-hosted runner。
当前 workflow 不使用 `--provenance`。

## GitHub Release 与不可变版本

npm 发布成功后，同一个 job 执行等价于以下命令的操作：

```bash
gh release create "v$VERSION" \
  --repo oldwinter/herdr-orchestrator \
  --target "$GITHUB_SHA" \
  --title "v$VERSION" \
  --generate-notes
```

这会让 `v<version>` GitHub Release 指向实际发布的 `main` 提交。npm 版本不可变：

- 已存在版本不能覆盖、修补或重发；
- 只改运行时代码而不增加 `package.json` 版本，只会得到 release-plan no-op；
- 已发布版本有缺陷时，应提交修复并发布一个新 SemVer，而不是尝试替换旧 tarball。

## 失败恢复

| 失败位置 | 状态判定 | 恢复方式 |
| --- | --- | --- |
| 编译或质量 gate | 未进入 release-plan；registry 未改变 | 根据完整质量摘要修复，运行 `just check`，再通过正常提交触发 CI |
| registry 查询 | `npm_registry_query_failed`，退出码 `2` | 确认 registry 可用后重跑；不要手工把 plan 改成 `publish=true` |
| hosted runner、Environment 或 OIDC | 版本仍缺失时没有发布 | 修复 GitHub-hosted runner、Environment `npm` 或 npm Trusted Publisher 配置后重跑 |
| `npm publish` 之前 | registry 仍缺少版本 | 重跑后 release-plan 仍会选择同一确切版本 |
| npm 已成功、GitHub Release 创建失败 | registry 已有版本，整条 workflow 重跑会成为 no-op | 核验 npm 版本与发布提交，再用上面的 `gh release create` 命令补建缺失 Release |
| 已发布版本包含缺陷 | npm 版本不可变 | 回滚使用方或停止采用该版本；提交修复并发布新的 patch/minor/major 版本 |
| 目标仓库升级返回 `preserved` | 用户修改内容未被覆盖 | 逐项审查并手工合并；在内容与 manifest 重新一致后运行 `doctor`，不要强删本地状态 |

若 npm 已成功但需要手工补建 GitHub Release，`GITHUB_SHA` 必须是当次 npm 发布所对应的
确切提交，而不是当前分支上任意更新的提交。

## 不自动部署生产

`.github/workflows/ci.yml` 自动化的是测试、npm 分发与 GitHub Release，不会：

- 在任何使用方仓库运行 `install`、`upgrade`、`run` 或 `dashboard`；
- push、merge、部署服务、修改生产数据或授予生产权限；
- 把 workflow 生成的本地控制面状态提交到 Git；
- 将 harness 的高自动化启动参数解释为生产操作授权。

生产采用、升级和运行必须由目标环境的责任人另行显式执行，并遵守项目的安全边界。

## 延伸阅读

- [安装与分发系统](systems/installation-and-distribution.md)
- [安全边界与报告](security.md)
- [如何贡献](how-to-contribute/index.md)
- [依赖参考](reference/dependencies.md)
