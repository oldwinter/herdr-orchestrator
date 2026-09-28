# Devin factory lane

`scripts/devin_factory.py` is a local software-factory lane built on the
canonical durable queue. It reuses `Store`, `Coordinator`, the attempt ledger,
fencing, dedupe contracts, retry/backoff, and completion verification — the
only difference from a Herdr dispatch is transport: an injected
`LocalDispatcher` runs a backlog item's declared acceptance checks inside the
workflow workspace instead of prompting an agent.

Dedicated configuration: `workflows/devin-factory.toml` — its own state
database (`.orchestrator/factory/state.db`) and `codex`/`claude` workers.
Receipts follow the enqueue contract: intake attaches a
`TaskReceipt(kind=file)` to each job, so an outcome can only succeed with a
verified file receipt at the item's declared path.

## Run

```sh
just factory-validate    # dry-run: parse backlog, print items, touch nothing
                         # (warns when a check timeout exceeds the agent budget)
just factory-intake      # enqueue backlog items (idempotent)
just factory-run         # drain pending items through their checks
just factory-status      # queue counts, job states, unqueued items
just factory-report      # write .orchestrator/factory/report.md
```

`factory-run` accepts `--once` (single claim wave) and
`--drain-timeout-seconds N` (default 3600), e.g.
`just factory-run --once`. The drain deadline bounds new claims and
truncates in-flight dispatches via the canonical `dispatch_deadline`.

Both run modes emit JSON with a `jobs` list — one entry per queued job
(`job_id`, `dedupe_key`, `state`, `task_verified`, `error_code`) — so a
failed or retried item is identifiable from the run output itself without
a follow-up `factory-status` call.

Backlog items live in `factory/backlog.toml` (`schema_version = 1`). Each
item declares `dedupe_key`, `title`, `harness`, `prompt_file`, `receipt`,
optional `max_attempts` (1–8, overriding the workflow default for this job
only; existing jobs keep the budget they were enqueued with), an optional
`requires = [dedupe_key, ...]` list,
and a non-empty `[[items.checks]]` list of bounded argv+timeout commands
executed without a shell. An item only enters the queue at intake once every
`requires` entry names a backlog item whose job is already `succeeded`;
until then intake reports it under `waiting` (unknown references, duplicates
and cycles are rejected as `factory_requires_*` errors). Re-run
`just factory-intake` after a requirement lands — `factory-run` only drains
jobs already enqueued. Check exit codes and
stdout/stderr are recorded in per-attempt evidence under
`.orchestrator/factory/evidence/<dedupe_key>/` (ignored by Git along with
everything under `.orchestrator/`).

## Inspect

- `just factory-status` — durable queue state for the `devin-factory`
  workflow, including `task_verified` and `verification_class`; the backlog
  section lists `unqueued` items and `waiting` items with each unmet
  requirement's current queue state (a `failed` blocker will never
  release its dependents without operator intervention).
- `just factory-report` — operator report with one row per job, a link to
  its latest evidence file, and a backlog coverage section.
- Raw inspection works with the standard CLI against the same DB:
  `PYTHONPATH=src uv run python -m herdr_orchestrator status --workflow
  workflows/devin-factory.toml`.

## Verify

```sh
just test-devin-factory        # focused regression suite
uv run pytest tests/test_devin_factory.py -q
```

## Recover

- Retryable failures (check failure, timeout, stale/missing receipt) release
  the job back to `pending` with the attempt `abandoned` until
  `max_attempts` (default 2) is exhausted, after which the job is `failed`
  with the attempt `outcome_committed`.
- `just factory-run` again to drain released retries.
- `just factory-retry JOB_ID` re-queues a terminal failure with extra
  attempts after a fix; `just factory-gc` runs the canonical agent
  collector against the factory queue (dry-run unless `--apply`; the local
  lane creates no Herdr agents, so it normally reports no candidates).
- `blocked` remains a manual state per repository rules and requires an
  explicit resume; the factory lane does not write it.

## Stop

- `factory-run` is a bounded batch: it exits when the queue is idle or the
  `--drain-timeout-seconds` budget is hit. Send SIGINT to stop mid-run;
  claimed attempts expire on their lease and become resumable on the next
  run.
- Intake/status/report are single-shot and need no shutdown.

## Approval boundaries

The lane never merges, pushes, publishes, deploys, or modifies permissions.
A `succeeded` job means only that its declared checks passed and its file
receipt verified; merging, deployment, purchases, destructive actions, and
expanded permissions all still require explicit operator approval.
