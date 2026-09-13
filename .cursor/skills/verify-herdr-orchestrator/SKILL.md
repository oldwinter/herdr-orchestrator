---
name: verify-herdr-orchestrator
description: Drive herdr-orchestrator's isolated durable-queue CLI and local read-only Dashboard (127.0.0.1 Web UI) to prove operator-facing behavior. Use when verifying catalog, seed/status/enqueue, or the operations console against a throwaway workflow — never the user's `.orchestrator/state.db`.
---

# Verify herdr-orchestrator

给下一个从没见过这个仓库的 agent：这是本地优先的多 harness 控制面。操作者真正用手点的主界面是 **Dashboard**（只读 Web 投影）。改变 queue 的路径是 **CLI**（`seed` / `status` / `enqueue` / `catalog`）。`run`、`smoke`、`just doctor`、`manager`、`deliver` 会碰 Herdr pane 或真实 harness，不在本 skill 的默认验证环里。

feature map 在 `features/`。一次证明至少覆盖 map 里列出的全部入口；只打一个方便入口不算完整。本 skill 自带的可执行证明是 `dashboard-queue-board`。

## 隔离（先读）

默认 `just seed` / `just dashboard` / `just enqueue` 写的是 `workflows/multi-harness.toml` → `.orchestrator/state.db`。那是操作者自己的 queue。

**禁止**把验证接到那条路上。接不上隔离实例就停，不要去开 `http://127.0.0.1:8765`，不要杀名为 `python` / `chrome` 的进程。

隔离实例可以和用户的 Dashboard 并存：不同 `state_db`、OS 分配端口、workflow 名固定为 `verify-orchestrator`。seed 标题是 `Verify droid inventory` 和 `Verify codex architecture`，用来确认你没有读到用户 queue。

## Launch

从仓库根目录：

```bash
.cursor/skills/verify-herdr-orchestrator/helpers/launch.sh
```

它会：

1. 在 `.orchestrator/verify-scratch/<run-id>/` 写出 workflow TOML 和空 SQLite。
2. 对**该** workflow 跑 `seed`（期望 `{"added": 2, "existing": 0}`）。
3. 启动 `python -m herdr_orchestrator dashboard --host 127.0.0.1 --port 0 --poll-seconds 1`。
4. 等 stdout 出现 `{"status":"dashboard_started","url":"http://127.0.0.1:<port>"}`，并且 `GET /api/health` 的 `ok` 为 true。

就绪信号：helper 打印 `run.json`（含 `url`、`pid`、`state_db`），并把 run id 写入 `.orchestrator/verify-scratch/current`。证据副本在 `.orchestrator/verify-evidence/<run-id>/launch.json`。

没有 `uv` 时 helper 用 `PYTHONPATH=src python3`；有 `uv` 时用 `uv run python`。运行时零第三方依赖，Python 3.12+ 即可。不要为了验证去跑 `just doctor`（那会做真实 harness probe）。

指定已有 run：`HERDR_VERIFY_RUN=<run-id>`。

## Doctor

任何东西看起来不对时先跑：

```bash
.cursor/skills/verify-herdr-orchestrator/helpers/doctor.sh
```

只回答「这个实例还能不敢开」。通过条件：

- `run.json` 指向的 pid 仍在，且 `/proc/<pid>/cmdline` 含 `herdr_orchestrator` 和 `dashboard`。
- `state_db` / workflow 不是仓库默认那一对。
- `GET <url>/api/health` → `ok: true`。
- `GET <url>/api/snapshot` 的 `snapshot.workflow` 是 `verify-orchestrator`，`source_health.queue` 是 `ok`，两张 seed 卡都在且为 `pending`。
- 同 workflow 的 `status` JSON 与 snapshot 一致。

`source_health.herdr != ok`（常见 `herdr_cli_missing` / `not_in_herdr`）**不是**失败。本机没有 Herdr 时 topology 空、页面顶部会出现 `Herdr observation unavailable: …`。那是诚实投影，不要为了消警告去绑用户的 Herdr session。

失败就 `cleanup.sh` 再 `launch.sh`，不要改用户的 8765 实例。

## Drive

### CLI（状态怎么进来）

一律带 launch 写下的 `--workflow <scratch>/workflow.toml`：

```bash
# 用 launch 已写好的路径；不要用 just catalog / just seed
PYTHONPATH=src python3 -m herdr_orchestrator catalog --workflow "$WORKFLOW" --format json
PYTHONPATH=src python3 -m herdr_orchestrator status --workflow "$WORKFLOW"
PYTHONPATH=src python3 -m herdr_orchestrator enqueue --workflow "$WORKFLOW" \
  --harness droid --title "Verify extra enqueue" \
  --prompt-file workflows/prompts/droid-inventory.md \
  --dedupe-key "verify-extra-enqueue-v1"
```

`enqueue` 必须显式 `--harness`。省略时主控会选 worker，在这个环境里通常不可用。不要跑 `enqueue-auto`、`run`、`run-once`、`retry`、`resume`、`gc --apply`。

稳定输出：

- `catalog --format json`：`harnesses[].harness` 含 `droid` `grok` `codex` `pi` `claude` `hermes`。
- `status`：`workflow` 为 `verify-orchestrator`，`counts.pending` ≥ 2，jobs 带 seed 标题。
- `enqueue`：`{"created": true, "harness": "droid", "job_id": <int>}`。

### Dashboard（操作者怎么读）

浏览器打开 `run.json` 的 `url`（只有 loopback）。页面 `<title>` 是 `Herdr Operations`。先等 `#connection-label` 变成 `Live` 或 `Connected`，且 `#metric-pending` 不再是 `—`。

稳定 handle（来自 `src/herdr_orchestrator/dashboard/static/index.html` 与 `dashboard.js`，不要用坐标）：

| 区域 | handle |
| --- | --- |
| 工作流名 | `#workflow-name` → `verify-orchestrator` |
| 连接 | `#connection-label` |
| 摘要 | `#metric-running` `#metric-attention` `#metric-pending` `#metric-agents` `#metric-worktrees` `#metric-succeeded` |
| 源警告 | `#source-warning-region` / `#source-warning` |
| 看板 | `#kanban`，列 `#kanban-column-queued`（`data-column-key="queued"`）等 |
| 任务卡 | `article.job-card[data-job-id]`，标题在 `h3` |
| Attention | `#attention-list`，条目 `[data-attention-id]` |
| 时间线 | `#timeline`，事件 `[data-event-id]` |
| 拓扑 | `#topology`（`aria-label="Interactive Herdr topology (read-only)"`），空态 `#topology-empty` |
| 缩放 | `#topology-zoom-in` `#topology-zoom-out` `#topology-fit` |

窄屏（≤760px）才有 `#kanban-navigation button[data-column-key]`。验证默认用 1440px 宽。

机器旁路（文档里的公开 HTTP，不是 test-only）：

| 路径 | 用途 |
| --- | --- |
| `GET /` | 页面 |
| `GET /api/health` | monitor 是否已有 snapshot |
| `GET /api/snapshot` | `{event_id, snapshot}` |
| `GET /api/events` | SSE `event: snapshot` |

没有 POST。不要用 `Store()` 或测试 fake projector 插数据。

证明看板：

```bash
.cursor/skills/verify-herdr-orchestrator/helpers/drive-dashboard-board.sh
```

内部会再跑 doctor、用本机 Chrome dump-dom/截图，并断言 seed 标题同时出现在 snapshot 和 DOM。

截图单独重跑：

```bash
.cursor/skills/verify-herdr-orchestrator/helpers/capture-dashboard.sh
```

Chrome 需要 `--no-sandbox`（容器里）。每个 run 用自己的 `--user-data-dir`，避免碰到操作者的浏览器 profile。

## Evidence

证据根目录：`.orchestrator/verify-evidence/<run-id>/`（`.orchestrator/` 已 gitignore，不要提交）。cleanup **不得**删除这个目录。

最低证明标准：

- 走真实操作者路径：CLI `seed`/`enqueue` 写入 queue，浏览器打开 Dashboard 读投影。不要内部 setter，不要只信 unittest。
- 同时留下动作和结果：`seed.json` / `status.json` **以及** 截图 + dump-dom + `/api/snapshot`。
- 副作用：隔离 `state.db` 里能查到对应 `jobs` 行；默认 `.orchestrator/state.db` 不出现这些 `dedupe_key`。
- 无 Herdr 时，记录 `source_health.herdr` 和页面警告，不要假装 topology 已验证。
- 截图必须能看出 Queued 列里的 seed 标题，不能只拍空白 loading。

常见文件：`launch.json` `seed.json` `health.json` `status.json` `snapshot.json` `doctor.json` `page.html` `board.png` `drive-dashboard-board.json`。

## Cleanup

```bash
.cursor/skills/verify-herdr-orchestrator/helpers/cleanup.sh
```

只 SIGTERM/SIGKILL `run.json` 里那个 pid，且 cmdline 必须匹配我们的 dashboard。然后删除 `.orchestrator/verify-scratch/<run-id>/`，保留 evidence。若 `current` 指向该 run，一并清掉。

失败的 launch/drive 也要跑 cleanup，避免占着端口和 scratch。需要保留现场时先把 evidence 目录复制到 `/tmp` 再清理。

## Helpers

全部可执行，从仓库根调用：

| 脚本 | 作用 |
| --- | --- |
| `helpers/launch.sh` | 隔离 seed + Dashboard |
| `helpers/doctor.sh` | 只读健康检查 |
| `helpers/drive-dashboard-board.sh` | 证明看板 |
| `helpers/capture-dashboard.sh` | Chrome 截图 + dump-dom |
| `helpers/cleanup.sh` | 拆掉本 run，保留证据 |
| `helpers/common.sh` | 被 source，不要直接跑 |

## 其他表面（本 skill 不默认开）

- Durable queue 调度：`just run` / `run-once` / `run-until-idle` 需要 `HERDR_ENV=1` 和已登录 harness。
- `just doctor` / `readiness-matrix` / `smoke`：真实 harness turn。
- `just manager`：当前 Herdr session 的交互管理，不模拟 queue。
- `just deliver`：opt-in 标准交付，只有用户明确要求时才走。

这些要另开 feature 文件和独立隔离策略后再验证。
