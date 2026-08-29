# 维护者
Active contributors: oldwinter, chendongdong

本页把 `.github/CODEOWNERS` 的正式 review ownership 与 `origin/main` 的近期人类贡献记录并列展示。仓库当前有 oldwinter 和 chendongdong 两名可核验的人类贡献者，因此保留本页；不按 commit 数、代码量或其他指标排名。

## 数据口径

- 正式 owner 来自 `.github/CODEOWNERS`。
- Recent contributors 与 last activity 来自 `git log origin/main -- <path>`，不是当前工作分支。
- `dependabot[bot]`、`factory-droid[bot]` 等 bot 账户被排除。
- Git 历史只能说明谁近期改过对应路径，不能证明平台权限、值班安排、雇佣关系或长期责任。
- 表中的日期是 `origin/main` 在对应 scope 的最近提交日期；本次快照中各 scope 最近活动均为 2026-08-28。

## 正式 CODEOWNERS

`.github/CODEOWNERS` 当前的 fallback 和全部具体 pattern 都指向 `@oldwinter`：

| Pattern | 正式 owner | 含义 |
| --- | --- | --- |
| `*` | `@oldwinter` | 仓库默认 review owner |
| `/.github/` | `@oldwinter` | CI、Dependabot、CODEOWNERS 等仓库自动化 |
| `/scripts/` | `@oldwinter` | 生成、质量和发布脚本 |
| `/src/herdr_orchestrator/` | `@oldwinter` | Python 控制面 |
| `/workflows/` | `@oldwinter` | 声明式 workflow 与 prompt |

CODEOWNERS 不会自动证明 branch protection 必须请求该 owner，也不会描述 npm Trusted Publisher、GitHub Environment 或 repository admin 权限。这些设置必须在对应平台核验。

## 子系统 ownership 图

下表不排名贡献者；同一单元格只列出 `origin/main` 中可核验的近期人类参与者。

| 子系统 | 完整路径 | 正式 owner | Recent contributors | Last activity |
| --- | --- | --- | --- | --- |
| Coordinator、queue、Herdr、Dashboard、delivery | `src/herdr_orchestrator/` | `@oldwinter` | oldwinter, chendongdong | 2026-08-28 |
| Workflow、profile 与文档 schema | `workflows/`、`profiles/`、`docs/` | `@oldwinter`（`workflows/` 显式，其余由 `*`） | oldwinter, chendongdong | 2026-08-28 |
| Python/npm 分发与 manager | `pyproject.toml`、`package.json`、`package-lock.json`、`packages/`、`bin/`、`scripts/` | `@oldwinter` | oldwinter, chendongdong | 2026-08-28 |
| CI、发布与安全配置 | `.github/`、`SECURITY.md` | `@oldwinter` | oldwinter, chendongdong | 2026-08-28 |
| 贡献指南与 Wiki | `AGENTS.md`、`CONTRIBUTING.md`、`droid-wiki/` | `@oldwinter`（由 `*`） | oldwinter, chendongdong | 2026-08-28 |

`chendongdong` 是近期核心贡献者，但当前没有单独的 CODEOWNERS pattern。若团队要建立 backup owner 或双人 review，必须修改 `.github/CODEOWNERS` 和相应平台规则；不能只在 Wiki 中宣称已经存在。

## 维护责任

| 范围 | 需要守住的契约 | 相关页面 |
| --- | --- | --- |
| Durable queue | SQLite v1→v4 migration、lease、attempt、retry/resume、receipt fail-closed | [数据模型](reference/data-models.md)、[Coordinator](systems/coordinator-and-queue.md) |
| Workflow/catalog | Schema v1、六 harness allowlist、compact/full profile 分层、planner 受限输出 | [配置](reference/configuration.md)、[Catalog 与路由](systems/catalog-and-routing.md) |
| 分发 | Python/npm 版本同步、package files、manifest ownership、manager 固定 argv 转发 | [依赖](reference/dependencies.md)、[安装与分发](systems/installation-and-distribution.md) |
| 发布/CI | Registry version gate、GitHub-hosted publish、OIDC、无长期 npm token | [部署](deployment.md)、[开发工具](how-to-contribute/tooling.md) |
| Dashboard/observability | 只读投影、loopback、脱敏、外部 exporter 默认关闭 | [Dashboard](systems/dashboard.md)、[安全](security.md) |
| Wiki/贡献 | 命令与实现同步、完整源码路径、运行态和敏感数据不入 Git | [如何贡献](how-to-contribute/index.md) |

## 变更与发布检查

### 合入前

- 先运行最小相关测试，收口前运行 `just check`。
- Workflow 字段变化同步 `docs/workflow-schema.md` 和相关行为测试。
- CLI parser 变化运行 `just docs-generate` 与 `just docs-check`，不要手改 `docs/generated/cli.md`。
- SQLite 变化保持顺序 migration 和旧数据库兼容。
- 分发变化核对 `pyproject.toml`、`package.json`、`package-lock.json`、`packages/herdr-manager/package.json` 与 `src/herdr_orchestrator/__init__.py` 的版本/依赖关系。
- `git status` 中不得出现 `.orchestrator/`、原始 prompt、终端输出、凭据或真实 `.env`。

### 发布边界

- `scripts/npm-release-plan.mjs` 的 registry 查询必须 fail closed；已存在版本仍是成功 no-op。
- npm publish 必须留在 GitHub-hosted runner，使用 OIDC `id-token: write`；不得引入长期 npm token。
- Self-hosted runner 不能执行不可信 pull request 代码。
- npm 版本不可覆盖；若 npm 已发布而 GitHub Release 缺失，应核验原提交后补建 Release。
- npm/GitHub Release 是包发布，不等于生产部署。

更完整的本地与 CI 命令见[开发工作流](how-to-contribute/development-workflow.md)和[部署](deployment.md)。

## 安全报告与升级

`SECURITY.md` 指定 GitHub private vulnerability reporting：

<https://github.com/oldwinter/herdr-orchestrator/security/advisories/new>

不要在公开 issue、普通 handoff 或 Git 中放入 token、登录态、原始 prompt、完整终端输出、私密 exploit 细节或真实 `.env`。涉及 secret、production、权限修改或无法确认的数据暴露时，停止自动处理并升级给拥有对应平台权限的人。仓库没有声明安全邮箱、聊天频道或值班表，本页不补写不存在的联系方式。

## Handoff 最小信息

维护 handoff 应记录：

1. 受影响系统和完整仓库根相对路径。
2. 当前 branch/commit、包版本与最后通过的 gate。
3. 已执行命令、退出码、脱敏证据位置和失败原因。
4. 不得破坏的 schema、migration、receipt、manifest ownership 与安全边界。
5. 下一步、预期结果、回滚点和所需平台权限。

## 关键源文件

| 完整路径 | 用途 |
| --- | --- |
| `.github/CODEOWNERS` | 正式 review ownership |
| `.github/workflows/ci.yml` | CI、release gate 与 npm OIDC 发布 |
| `SECURITY.md` | 支持范围和私密漏洞报告入口 |
| `AGENTS.md` | 仓库开发与安全约束 |
| `CONTRIBUTING.md` | 贡献流程 |
| `scripts/npm-release-plan.mjs` | npm registry version gate |
| `package.json`、`package-lock.json` | 根 npm 发布元数据 |
| `packages/herdr-manager/package.json` | Manager 薄包发布元数据 |
