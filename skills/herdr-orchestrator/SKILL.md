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
npm exec --yes --package=herdr-orchestrator -- herdr-orchestrator install --project .
```

Version 0.1.7 published two bin names pointing to the same file, so npm could select
`herdr-manager` for an orchestrator command. Use the explicit package/bin form for existing
installs, then pin runtime calls to the installed manifest version:

```bash
HERDR_VERSION="$(node -p "require('./.herdr-orchestrator/manifest.json').version")"
herdr_orchestrator() {
  npm exec --yes --package="herdr-orchestrator@$HERDR_VERSION" -- herdr-orchestrator "$@"
}
```

Keep the helper name distinct from native `herdr`, which owns `herdr agent` commands.

The installer selects locally available harness CLIs. To choose explicitly, repeat
`--harness`, for example `--harness droid --harness codex`.

Always run diagnostics before real dispatch:

```bash
herdr_orchestrator doctor --project . --probe-timeout-seconds 30
```

Continue only when top-level `ok`, `installation.ok`, and `runtime.ok` are true and each
selected harness has `readiness:<harness>.status = ready`. `NOT VERIFIED` is not readiness
evidence. Keep manifest-managed files unchanged; doctor reports edited files as modified.

Read the compact catalog before choosing a worker or using automatic routing:

```bash
herdr_orchestrator catalog --project . --format text
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

Inspect both the durable queue and live agents before dispatch:

```bash
herdr_orchestrator status --project .
herdr agent list
```

Account for pending, running, blocked, and attention jobs before draining. If the queue belongs
to unrelated work, install a separate controller project to get a separate state database.
Preserve the old queue for audit. For dependent review and validation, dispatch one job at a
time and inspect its report before enqueueing the next. Do not reuse a pane whose prior turn
is still working or unresolved.

Write the complete task contract to a UTF-8 file in the target repository before enqueueing.
Use an ignored runtime path when the prompt should not be committed:

```bash
mkdir -p .orchestrator/requests .orchestrator/results
$EDITOR .orchestrator/requests/inspect-readme.md
```

For a read-only pane task, prefer a fresh file receipt:

```bash
test ! -e .orchestrator/results/inspect-readme-v1.receipt || exit 1
herdr_orchestrator enqueue --project . \
  --harness pi \
  --placement pane \
  --title "Inspect README" \
  --prompt-file .orchestrator/requests/inspect-readme.md \
  --dedupe-key inspect-readme-v1 \
  --receipt-file .orchestrator/results/inspect-readme-v1.receipt
```

Before enqueueing, verify that the receipt path does not exist. Require the worker to finish
the report and write the non-empty receipt as its final action. For pane/tab placement, put
the exact absolute receipt path in the prompt and pass its execution-root-relative form to
`--receipt-file`. When inspecting another repository, the execution root still owns the receipt.

Use `--receipt-prefix` only when terminal-only evidence is required. If a prompt line starts
with the expected prefix, verification fails as `task_receipt_ambiguous`; an echoed instruction
cannot prove authorship.

Use `--placement tab` for an isolated tab. Use `--placement worktree` for repository writes
and require a non-empty receipt file relative to that worktree's execution root:

```bash
herdr_orchestrator enqueue --project . \
  --harness grok \
  --placement worktree \
  --title "Implement focused change" \
  --prompt-file .orchestrator/requests/implement-change.md \
  --dedupe-key implement-change-v1 \
  --receipt-file .orchestrator/implement-change-v1.receipt
```

For worktree placement, the execution root is assigned during provisioning. Instruct the
worker to resolve the same relative receipt path from that assigned root, create its parent
directory, and write it last. An absolute path to the source checkout would target the wrong root.

`--placement auto` uses the workflow topology policy. The explicit values are:

```text
--placement pane
--placement tab
--placement worktree
```

For automatic worker selection, constrain the controller and candidate pool deliberately:

```bash
test ! -e .orchestrator/results/inspect-agents-v1.receipt || exit 1
herdr_orchestrator enqueue --project . \
  --harness auto \
  --controller-harness pi \
  --worker-harness pi \
  --worker-harness grok \
  --placement pane \
  --title "Inspect agent instructions" \
  --prompt-file .orchestrator/requests/inspect-agents.md \
  --dedupe-key inspect-agents-v1 \
  --receipt-file .orchestrator/results/inspect-agents-v1.receipt
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
herdr_orchestrator run --project . \
  --until-idle \
  --drain-timeout-seconds 86400
```

The result separates `claimed`, cumulative `batch`, and global `queue`. When workers are
narrowed, read `worker_pool_idle` and `queue_idle` separately. Use `--once` only when one
replica-limited wave is intentional. `seed` can be a successful no-op when the installed
workflow has no `seed_jobs`.

```bash
herdr_orchestrator status --project .
```

When `agent_not_settled`, `agent_turn_not_observed`, `herdr_timeout`, or
`task_receipt_missing` occurs, inspect the native agent and the expected report/receipt:

```bash
herdr agent get <agent-name>
herdr agent read <agent-name> --source recent-unwrapped --lines 120
```

Re-read live state until the prior turn has settled. If the bounded investigation ends without
that evidence, stop and leave the job unresolved. Never retry or enqueue replacement work while
the original agent is working or may still write the receipt; a transient idle snapshot is not
proof. A late receipt does not retroactively make a failed job successful. Preserve its failed
attempt and report the late artifact separately.

Never retry blocked jobs. If `attempt_phase=attention`, stop: neither retry nor resume is
supported, and replacement prompts are forbidden. An accepted unresolved timeout enters this
state. Preserve it for operator investigation.

For an ordinary blocked agent question, with a phase other than attention, explicit human review
and the supplied response can resume the same agent, pane, and attempt:

```bash
herdr_orchestrator resume --project . --job-id 43 --response-file approval.txt
```

For an exhausted failed job whose prior turn is confirmed settled and whose file receipt is
still absent, retain its dedupe identity and add attempt budget:

```bash
herdr_orchestrator retry --project . --job-id 42 --extra-attempts 1
```

If the receipt already exists or the contract must change, first confirm the original turn has
settled and review any late artifact. Keep the old attempt. If more work is needed, enqueue a
new task with a new dedupe key and a fresh receipt path. An unchanged existing file fails as
`task_receipt_stale`.
For `task_receipt_ambiguous`, replace the prefix contract with a file receipt.

Inspect the installed workflow's `agent_timeout_seconds` before long work. The npm installer
in 0.1.7 generates 300 seconds; source checkout workflows may use a longer budget. The wrapper
does not expose workflow, state DB, lease, or agent-timeout overrides. A longer
`--drain-timeout-seconds` only extends the drain loop, not the configured agent deadline.
Keep installed turns small by collecting long validation logs first, then asking Grok to audit
them and run short independent probes. This reduces exposure to background-task transitions;
a transient idle snapshot alone does not establish the cause of a missing receipt.
For reviews that need a longer turn, use the source checkout's documented workflow/just
configuration. Put the untracked workflow under `.orchestrator/`, choose its own state DB,
and set `lease_seconds >= agent_timeout_seconds + 90`. A tracked example may share an existing
queue. Keep managed installer files unchanged.

Preview cleanup of succeeded agent panes, then apply only when cleanup is requested:

```bash
herdr_orchestrator gc --project . --succeeded-agents
herdr_orchestrator gc --project . --succeeded-agents --apply
```

GC preserves every worktree workspace, checkout, and branch. A candidate needs a persisted
`member_reused=false` creation receipt and the current pane ID must still match that receipt.
It also refuses active, foreign, or wrong-workspace agents. A tab-placed task still closes only
its verified agent pane, never the containing tab.

Start the read-only operations view when a live view is useful:

```bash
herdr_orchestrator dashboard --project .
```

Its default URL is `http://127.0.0.1:8765`.

## 4. Judge completion

The orchestrator must run from a Herdr pane for real dispatch. A terminal `succeeded` job with
`task_verified = true` satisfies its declared machine receipt. When no receipt was declared,
`task_verified = null`: inspect the requested artifact before claiming the task is complete.
For review and validation, also read the report and require the requested verdict. Queue idle
only describes scheduling; it cannot replace `succeeded`, `task_verified=true`, and the verdict.

`blocked`, `unknown`, timeout, a pane that merely exists, or `idle` / `done` without the
declared receipt are not task success. Use `error_code` and bounded `error_summary` from
`status`; keep unresolved work visible. Retry only eligible failed jobs; resume ordinary blocked
questions only through the explicit human-response flow. Attention remains halted.

Never push, merge, publish, send, delete worktrees, change permissions, or touch production
unless the user separately authorized that exact action.

Use `upgrade` for a requested runtime update and `uninstall` only when the user explicitly
asks to remove it. Both preserve user-modified managed files and report them in JSON.
