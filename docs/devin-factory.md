# Devin factory lane

`scripts/devin_factory.py` is a local software-factory lane built on the
canonical durable queue. It reuses `Store`, `Coordinator`, the attempt ledger,
fencing, dedupe contracts, retry/backoff, and completion verification — the
only difference from a Herdr dispatch is transport: an injected
`LocalDispatcher` runs a backlog item's declared acceptance checks inside the
workflow workspace instead of prompting an agent.

Dedicated configuration: `workflows/devin-factory.toml` — its own state
database (`.orchestrator/factory/state.db`), single `codex` worker, worker
name `factory`, and `[workflow.receipts] kind = "file"` so every job outcome
must be backed by a verified file receipt.

## Run

```sh
just factory-intake      # enqueue backlog items (idempotent)
just factory-run         # drain pending items through their checks
just factory-status      # queue counts, job states, unqueued items
just factory-report      # write .orchestrator/factory/report.md
```

`factory-run` accepts the standard coordinator flags as extra args, e.g.
`just factory-run --max-waves 1 --poll-interval-seconds 0`.

Backlog items live in `factory/backlog.toml` (`schema_version = 1`). Each
item declares `dedupe_key`, `title`, `prompt_file`, `receipt`, optional
`placement`/`max_attempts`, and a non-empty `[[items.checks]]` list of
bounded argv+timeout commands executed without a shell. Check exit codes and
stdout/stderr are recorded in per-attempt evidence under
`.orchestrator/factory/evidence/<dedupe_key>/` (ignored by Git along with
everything under `.orchestrator/`).

## Inspect

- `just factory-status` — durable queue state for the `devin-factory`
  workflow, including `task_verified` and `verification_class`.
- `just factory-report` — operator report with one row per job and a link to
  its latest evidence file.
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
- `PYTHONPATH=src uv run python -m herdr_orchestrator retry-failed
  --workflow workflows/devin-factory.toml` re-queues terminal failures after
  a fix; `... stuck` and `... gc` operate on the same state DB the standard
  way.
- `blocked` remains a manual state per repository rules and requires an
  explicit resume; the factory lane does not write it.

## Stop

- `factory-run` is a bounded batch: it exits when the queue is idle or
  `--max-waves` is hit. Send SIGINT to stop mid-run; claimed attempts expire
  on their lease and become resumable on the next run.
- Intake/status/report are single-shot and need no shutdown.

## Approval boundaries

The lane never merges, pushes, publishes, deploys, or modifies permissions.
A `succeeded` job means only that its declared checks passed and its file
receipt verified; merging, deployment, purchases, destructive actions, and
expanded permissions all still require explicit operator approval.
