# 维护者指南
Active contributors: oldwinter, chendongdong

本页区分三件事：仓库中声明的 review ownership、Git 历史中可核验的真实贡献，以及建议
采用的维护 handoff。建议不是尚未存在的组织制度；当前权威配置仍以
`.github/CODEOWNERS`、`.github/workflows/ci.yml`、`SECURITY.md` 和仓库实际权限为准。

## CODEOWNERS 现状

`.github/CODEOWNERS` 当前把以下范围全部指派给 `@oldwinter`：

| pattern | 当前 code owner |
| --- | --- |
| `*` | `@oldwinter` |
| `/.github/` | `@oldwinter` |
| `/scripts/` | `@oldwinter` |
| `/src/herdr_orchestrator/` | `@oldwinter` |
| `/workflows/` | `@oldwinter` |

这表示 `@oldwinter` 是声明式 review owner，包括安全敏感自动化；它不表示其他贡献者没有
维护贡献，也不能证明 GitHub 分支保护一定要求其批准。分支保护、Environment 审批和 npm
Trusted Publisher 权限必须在对应平台上另行核验，不能从 `CODEOWNERS` 推断。

## 可核验的贡献者

对全部可见 Git refs 执行只读 `git shortlog -sne --all` 和 `git log --all` 的结果显示：

- `oldwinter`：12 个作者记录；
- `chendongdong`：10 个作者记录；
- `dependabot[bot]`：6 个自动依赖更新记录。

因此，当前可核验的活跃人类贡献者是 `oldwinter` 和 `chendongdong`。Git 历史显示两人均
参与核心实现；其中 `chendongdong` 的作者记录覆盖拓扑执行、dashboard、一次性分发、
npm Trusted Publishing、恢复与 CI，`oldwinter` 的作者记录覆盖初始 orchestrator、
harness catalog、标准化交付、可信 dispatch、仓库 readiness、自动化启动及后续修复。
这些事实来自提交作者和主题，不等同于平台权限、雇佣关系或长期值班承诺。

## 关键系统 ownership 建议

目前只有一个 repository-wide code owner。为降低单点风险，后续 handoff 可以为每个
系统明确“主要维护人、备份维护人、验证命令和敏感边界”；在
`.github/CODEOWNERS` 或其他权威文件真正更新前，不应把建议写成既成事实。

| 系统 | 关键真源 | handoff 时应明确的责任 |
| --- | --- | --- |
| Coordinator 与 durable queue | `src/herdr_orchestrator/`、`tests/` | schema/SQLite 向后兼容、lease、重试、receipt、稳定错误语义 |
| Workflow 与 harness catalog | `workflows/multi-harness.toml`、`workflows/prompts/`、`profiles/harnesses/`、`docs/workflow-schema.md` | planner schema、启用 worker、紧凑/完整 profile 分层及 launch policy |
| npm/Python 分发 | `package.json`、`package-lock.json`、`pyproject.toml`、`bin/herdr-orchestrator.mjs`、`tests/test_distribution.py` | 版本同步、打包清单、manifest ownership、升级与卸载兼容 |
| Registry 与 release 自动化 | `scripts/npm-release-plan.mjs`、`.github/workflows/ci.yml`、`tests/test_release.py` | test gate、registry fail-closed、GitHub-hosted OIDC、GitHub Release 恢复 |
| Dashboard 与本地数据 | `src/herdr_orchestrator/dashboard/`、`docs/dashboard.md`、`docs/observability.md` | 只读语义、SSE/topology、脱敏、外部导出 fail-closed |
| 安全与依赖 | `SECURITY.md`、`.github/dependabot.yml`、`uv.lock`、`package-lock.json` | 私密披露、精确 pin、依赖审计、自动 security insight 的安全处理 |
| 贡献与文档 | `AGENTS.md`、`CONTRIBUTING.md`、`docs/`、`droid-wiki/` | 命令与实现同步、风险/回滚说明、禁止提交运行时与敏感内容 |

涉及 `.github/workflows/ci.yml`、`scripts/npm-release-plan.mjs`、
`bin/herdr-orchestrator.mjs`、安全数据处理或版本元数据的变更，建议至少由熟悉发布边界的
维护者复核；这是风险控制建议，不代表仓库当前已经配置了双人审批规则。

## 发布前检查表

### 变更与版本

- [ ] 变更有聚焦的行为测试，风险和回滚方式已写清楚。
- [ ] `package.json`、`package-lock.json`、`pyproject.toml` 与
      `src/herdr_orchestrator/__init__.py` 中的 `__version__` 使用同一个新 SemVer。
- [ ] 新版本尚未存在于 npm；`npm run release:plan` 输出 `publish=true` 和
      `reason=version_missing`。
- [ ] `npm pack --dry-run --json` 的文件清单只包含 `package.json` 允许的分发内容。
- [ ] 用户可见 CLI 变化已用 `just docs-generate` 更新 `docs/generated/cli.md`。
- [ ] telemetry 或 exporter 变化已同步 `docs/observability.md`、`.env.example` 和 feature
      flag 生命周期。

### 本地验证

- [ ] 已执行 `uv sync --locked`。
- [ ] 迭代期间已运行最小相关测试以及 `just lint`。
- [ ] 提交 review 前已运行 `just check`。
- [ ] 分发改动至少通过 `tests/test_distribution.py`；发布改动至少通过
      `tests/test_release.py`。
- [ ] `git status` 中没有 `.orchestrator/`、原始 prompt、终端输出、凭据或真实 `.env`。

### CI 与发布权限

- [ ] `.github/workflows/ci.yml` 的 compile 和六个质量 gate 全部成功；没有把
      `continue-on-error` 误读为允许失败。
- [ ] pull request 测试仍在 GitHub-hosted runner，未把不可信代码移到 self-hosted
      runner。
- [ ] `release-plan` 查询失败时仍是 fail-closed，已存在版本仍是成功 no-op。
- [ ] `publish` 仍在 GitHub-hosted `ubuntu-latest`，绑定 Environment `npm` 并拥有
      `id-token: write`。
- [ ] 没有引入 `NODE_AUTH_TOKEN`、长期 npm token、未审查的 action tag 或
      `npm publish --provenance`。
- [ ] npm Trusted Publisher 精确绑定
      `oldwinter/herdr-orchestrator`、`ci.yml` 和 Environment `npm`。
- [ ] 已确认 GitHub-hosted runner 可启动；账单或 spending limit 不会阻断本次发布。

### 发布后

- [ ] npm registry 中出现准确版本，且包内容与本地 dry-run 预览相符。
- [ ] GitHub Release `v<version>` 指向执行发布的确切 `main` commit。
- [ ] 如果 npm 已成功而 GitHub Release 失败，按部署页的恢复步骤补建 Release；不要尝试
      覆盖不可变 npm 版本。
- [ ] 没有把 npm/GitHub Release 误当作生产部署；使用方安装和生产运行仍需独立授权。

## 安全升级

`SECURITY.md` 声明：最新 npm release 和当前 `main` 接收安全修复。漏洞必须通过 GitHub
private vulnerability reporting 提交：

<https://github.com/oldwinter/herdr-orchestrator/security/advisories/new>

不要用公开 issue 提交凭据、prompt、终端输出、会话内容或 exploit 细节。维护者收到安全
报告后，应先控制披露范围，再核验受影响版本、可达路径、最小修复和回归测试。若修复需要
发布：

1. 在私密范围内保留最少、已脱敏的复现证据；
2. 修复当前 `main`，并判断最新 npm release 是否需要新的不可变 SemVer；
3. 运行 `just security`、最小回归测试和 `just check`；
4. 通过正常的 OIDC 发布链路发布新版本，不复用旧 npm 版本；
5. 在不泄露细节的前提下完成公告或 advisory 后续。

`.github/workflows/ci.yml` 在 `main` 的 security gate 失败时会创建或更新一个去重的
security insight issue。它是自动化告警，不是公开披露漏洞细节的通道；敏感证据仍应留在
private vulnerability reporting 中。

涉及 secret、生产访问、权限修改或无法确认的数据暴露时，必须停止推测并升级给具备相应
平台权限的人。仓库没有提供其他安全邮箱、聊天频道或值班表，本页不虚构这些联系方式。

## 维护 handoff

handoff 应让接手者能在不依赖隐性上下文的情况下恢复工作。建议在现有 issue、pull
request 或私密 advisory 中记录适合其敏感级别的以下内容：

1. **范围**：受影响系统以及准确的仓库根相对路径。
2. **状态**：当前 branch/commit、npm 版本、GitHub Release 状态和最后一个通过的 gate。
3. **证据**：执行过的命令、退出码、失败原因和已脱敏的 artifact 位置。
4. **不变量**：不得破坏的 schema、manifest ownership、SQLite/receipt 兼容与安全边界。
5. **下一步**：一个可执行动作、预期结果、回滚点和需要何种平台权限。
6. **未决风险**：registry、OIDC、runner、版本不可变性、用户修改文件或本地状态的影响。

以下内容不得进入普通 handoff 文本或 Git：

- npm/GitHub token、OIDC 凭据、登录态或真实 `.env`；
- 原始 prompt、完整终端输出、runtime state 或未脱敏 telemetry；
- private advisory 中尚未公开的 exploit 细节；
- 无法从配置、平台权限或 Git 历史核验的 ownership 承诺。

接手发布故障时，先区分“测试未通过”“registry 计划失败”“npm 未发布”和“npm 已发布但
GitHub Release 缺失”。最后一种情况重跑 workflow 会因 `version_exists` 成为 no-op，
必须核验原发布 commit 后补建 Release；详细命令见
[部署、发布与维护](deployment.md#失败恢复)。

## 延伸阅读

- [安装与分发系统](systems/installation-and-distribution.md)
- [安全边界与报告](security.md)
- [如何贡献](how-to-contribute/index.md)
- [依赖参考](reference/dependencies.md)
