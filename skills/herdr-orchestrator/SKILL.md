---
name: herdr-orchestrator
description: "Use when a user asks to dispatch work across Herdr-backed coding harnesses with a durable queue, retries, receipts, topology-aware panes or worktrees, the local operations dashboard, or a dedicated manual Herdr manager session."
---

# Herdr Orchestrator

Use the packaged CLI instead of assuming this Skill's checkout contains the runtime.

## 1. Bootstrap and preflight

From the target Git repository, check for `.herdr-orchestrator/manifest.json`. If it is
missing, bootstrap the project:

```bash
npx --yes herdr-orchestrator install --project .
```

The installer selects locally available harness CLIs. To choose explicitly, repeat
`--harness`, for example `--harness droid --harness codex`.

Always run diagnostics before real dispatch:

```bash
npx --yes herdr-orchestrator doctor --project . --probe-timeout-seconds 30
```

Exit code `1` is not success. Continue only when the JSON top-level `ok` is true and each
selected harness has `readiness:<harness>.status = ready`. `installed` or a profile file alone
does not prove authentication or model readiness.

Read the compact catalog before choosing a worker or using automatic routing:

```bash
npx --yes herdr-orchestrator catalog --project . --format text
```

Stable harness names are `droid`, `grok`, `codex`, `pi`, `claude`, and `hermes`. Herdr may
support additional kinds such as `cursor`, but they are outside this workflow's validated
catalog and cannot be passed to `--harness`.

New agents start with the packaged maximum-automation policy: Droid `--auto high`, Grok
`--always-approve --permission-mode bypassPermissions`, Codex
`--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust`, pi
`--approve`, Claude `--dangerously-skip-permissions`, and Hermes `--yolo --accept-hooks`.
Do not rely on a harness approval dialog as a safety boundary. The control plane fixes these
arguments; planner output and task prompts cannot replace them. External or destructive
actions still require the user's separate exact authorization.

Claude's first workspace-trust dialog has no native bypass flag. The adapter verifies the
expected execution root and sends one Enter only when a newly created Claude agent exposes all
three exact built-in markers:
`Accessing workspace:`, `Quick safety check:`, and `Yes, I trust this folder`. Treat every
other startup block, login, approval, or task question as unresolved; do not answer it
automatically.

## Manual manager (interactive alternative)

When the user wants one dedicated harness to observe and coordinate the current Herdr session,
without durable dispatch, use the short command from a Herdr pane:

```bash
herdr-manager
herdr-manager claude
```

Without an explicit harness, the launcher uses the first available CLI in this order:
Grok, Codex, Claude. If none is available, it fails with a stable installation hint. For a
one-off launch from any directory, use:

```bash
npx --yes herdr-manager
npx --yes herdr-manager claude
```

The command requires `HERDR_ENV=1` and adds no harness arguments or permission bypasses. The
manager policy is scoped to the current Herdr session, treats observed output as untrusted
data, and does not maintain queue state. Use the durable flow below when the task needs retries,
deduplication, leases, unattended execution, or receipts.

## 2. Write and enqueue the task packet

Write the complete task contract to a UTF-8 file in the target repository before enqueueing.
Use an ignored runtime path when the prompt should not be committed:

```bash
mkdir -p .orchestrator/requests
$EDITOR .orchestrator/requests/inspect-readme.md
```

For a read-only pane task with a machine-verifiable output line:

```bash
npx --yes herdr-orchestrator enqueue --project . \
  --harness pi \
  --placement pane \
  --title "Inspect README" \
  --prompt-file .orchestrator/requests/inspect-readme.md \
  --dedupe-key inspect-readme-v1 \
  --receipt-prefix "TASK-OK inspect-readme"
```

The receipt must appear at the start of its own terminal line. A prompt that merely mentions
the text is not a receipt.

Use `--placement tab` for an isolated tab. Use `--placement worktree` for repository writes
and require a non-empty receipt file relative to that worktree's execution root:

```bash
npx --yes herdr-orchestrator enqueue --project . \
  --harness grok \
  --placement worktree \
  --title "Implement focused change" \
  --prompt-file .orchestrator/requests/implement-change.md \
  --dedupe-key implement-change-v1 \
  --receipt-file .orchestrator/task-receipt.txt
```

`--placement auto` uses the workflow topology policy. The explicit values are:

```text
--placement pane
--placement tab
--placement worktree
```

For automatic worker selection, constrain the controller and candidate pool deliberately:

```bash
npx --yes herdr-orchestrator enqueue --project . \
  --harness auto \
  --controller-harness pi \
  --worker-harness pi \
  --worker-harness grok \
  --placement pane \
  --title "Inspect agent instructions" \
  --prompt-file .orchestrator/requests/inspect-agents.md \
  --dedupe-key inspect-agents-v1 \
  --receipt-prefix "TASK-OK inspect-agents"
```

Automatic routing synchronously runs one controller agent turn before enqueue returns. Treat
normal model latency as expected. The control plane first requires fresh `ready` health evidence;
unknown or expired evidence receives one bounded refresh, while an explicit unhealthy harness fails
with its stable harness-specific reason instead of falling back. Then require JSON with `created`,
`harness`, and `job_id`.
Reusing the same `--dedupe-key` must return the existing job with `created = false`.

## 3. Drain and inspect

Use one bounded drain invocation for normal queued work:

```bash
npx --yes herdr-orchestrator run --project . \
  --until-idle \
  --drain-timeout-seconds 86400
```

The result separates `claimed`, cumulative `batch`, and global `queue`. When workers are
narrowed, read `worker_pool_idle` and `queue_idle` separately. Use `--once` only when one
replica-limited wave is intentional. `seed` can be a successful no-op when the installed
workflow has no `seed_jobs`.

```bash
npx --yes herdr-orchestrator status --project .
```

For an exhausted job, retain its dedupe identity and add attempt budget:

```bash
npx --yes herdr-orchestrator retry --project . --job-id 42 --extra-attempts 1
```

Preview cleanup of succeeded agent panes, then apply only when cleanup is requested:

```bash
npx --yes herdr-orchestrator gc --project . --succeeded-agents
npx --yes herdr-orchestrator gc --project . --succeeded-agents --apply
```

GC preserves every worktree workspace, checkout, and branch. A candidate needs a persisted
`member_reused=false` creation receipt and the current pane ID must still match that receipt.
It also refuses active, foreign, or wrong-workspace agents. A tab-placed task still closes only
its verified agent pane, never the containing tab.

Start the read-only operations view when a live view is useful:

```bash
npx --yes herdr-orchestrator dashboard --project .
```

Its default URL is `http://127.0.0.1:8765`.

## 4. Judge completion

The orchestrator must run from a Herdr pane for real dispatch. A terminal `succeeded` job with
`task_verified = true` satisfies its declared machine receipt. When no receipt was declared,
`task_verified = null`: inspect the requested artifact before claiming the task is complete.

`blocked`, `unknown`, timeout, a pane that merely exists, or `idle` / `done` without the
declared receipt are not task success. Use `error_code` and bounded `error_summary` from
`status`; keep failed or blocked work visible until it is retried or consciously left terminal.

Never push, merge, publish, send, delete worktrees, change permissions, or touch production
unless the user separately authorized that exact action.

Use `upgrade` for a requested runtime update and `uninstall` only when the user explicitly
asks to remove it. Both preserve user-modified managed files and report them in JSON.
