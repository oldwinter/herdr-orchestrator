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
                         # (warns on unhonorable check timeouts, check
                         # executables absent from PATH, and durable-contract
                         # drift on already-queued items)
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
(`job_id`, `dedupe_key`, `harness`, `state`, `task_verified`,
`error_code`) — so a
failed or retried item is identifiable from the run output itself without
a follow-up `factory-status` call.

Backlog items live in `factory/backlog.toml` (`schema_version = 1`). Each
item declares `dedupe_key`, `title`, `harness`, `prompt_file`, `receipt`,
optional `max_attempts` (1–8, overriding the workflow default for this job
only; existing jobs keep the budget they were enqueued with), an optional
`requires = [dedupe_key, ...]` list,
and a non-empty `[[items.checks]]` list of bounded argv+timeout commands
executed without a shell. Per-check `timeout_seconds` (1–3600) defaults
to the item-level `check_timeout_seconds` (default 600). An item only
enters the queue at intake once every `requires` entry names a backlog
item whose job is already `succeeded`;
until then intake reports it under `waiting` (unknown references, duplicates
and cycles are rejected as `factory_requires_*` errors). Re-run
`just factory-intake` after a requirement lands — `factory-run` only drains
jobs already enqueued. Check exit codes and
stdout/stderr are recorded in per-attempt evidence under
`.orchestrator/factory/evidence/<dedupe_key>/` (ignored by Git along with
everything under `.orchestrator/`).

Editing a queued item: the durable contract covers `title`, `harness`,
`prompt_file` content and `receipt` — changing any of them makes the next
intake fail with `dedupe_contract_conflict` (retire the item by assigning a
new `dedupe_key` instead; `factory-validate` warns about the drift before
that). `checks`/`check_timeout_seconds` are not part of the contract and
apply live to the next attempt, `requires` only gates items not yet
queued, and `max_attempts` pins at enqueue.

## Inspect

- `just factory-status` — durable queue state for the `devin-factory`
  workflow, including `task_verified` and `verification_class`; the backlog
  section lists `unqueued` items and `waiting` items with each unmet
  requirement's current queue state (a `failed` blocker will never
  release its dependents without operator intervention). Job rows carry
  `retry_backoff_seconds` for backoff-deferred pending work,
  `lease_expired` for claims whose lease lapsed (reclaimable on the next
  drain), and `updated_at_utc` as the row's last state-change timestamp.
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
- When a required item fails terminally, dependents stay `waiting`
  forever (status/report show `key=failed`). After `factory-retry` lands
  the blocker at `succeeded`, re-run `just factory-intake` to release
  the dependents — intake is the only place the gate is evaluated.
- `just factory-retry JOB_ID` re-queues a terminal failure with extra
  attempts after a fix; `just factory-gc` runs the canonical agent
  collector against the factory queue (dry-run unless `--apply`; the local
  lane creates no Herdr agents, so it normally reports no candidates).
- A `running` job whose lease lapses mid-dispatch (crash, SIGINT) is
  reclaimed on the next drain; the local lane re-executes the declared
  checks on the same attempt (`recover` = re-dispatch — there is no
  interactive session to adopt), so the retry budget is not consumed.
  `just factory-status` flags such jobs with `lease_expired` while they
  wait for the lease to lapse.
- `blocked` remains an operator-attention state per repository rules; the
  factory lane only writes it when a reclaimed dispatch settles
  ambiguously (the canonical `attention` path). Local jobs have no pane
  to resume, so `just factory-retry JOB_ID` opts into
  `retry --allow-blocked` and re-drives the item on a fresh attempt. The
  canonical `just retry` keeps `failed`-only semantics for lanes whose
  blocked jobs may still own a live session.

## Stop

- `factory-run` is a bounded batch: it exits when the queue is idle or the
  `--drain-timeout-seconds` budget is hit. Send SIGINT or SIGTERM to stop
  mid-run: the runner exits 130 (SIGINT) or 143 (SIGTERM) and kills the
  in-flight check's whole process group immediately (checks run in their
  own session; descendant processes spawned by a check cannot outlive it
  and write artifacts after cancellation or a per-check timeout), and the
  claimed attempt's lease lapses so the job is reclaimed and re-dispatched
  on the next run.
- Intake/status/report are single-shot and need no shutdown.

## Approval boundaries

The lane never merges, pushes, publishes, deploys, or modifies permissions.
A `succeeded` job means only that its declared checks passed and its file
receipt verified; merging, deployment, purchases, destructive actions, and
expanded permissions all still require explicit operator approval.
