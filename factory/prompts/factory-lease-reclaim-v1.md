# factory-lease-reclaim-v1

Implement lease-expiry recovery for the local factory dispatcher.

A `running` job whose lease lapses mid-dispatch (crash, SIGINT) is
reclaimed by the canonical claim path. The reclaim enters the
coordinator's `recover` route; without a `recover` implementation the
outcome is `unsafe_turn_adoption` and the job lands in terminal
`blocked` even though nothing is actually wrong.

Add `LocalDispatcher.recover` with the canonical signature. Local
acceptance checks hold no interactive session state, so recovery is a
fresh bounded dispatch of the same checks on the same attempt — the
retry budget is not consumed.

Acceptance:

- `tests/test_devin_factory.py` gains a regression that claims a job,
  expires `jobs.lease_until` and `job_attempts.lease_until`, drains, and
  asserts the job reaches `succeeded` with `task_verified` and
  `attempts == 1` instead of `blocked`.
- `docs/devin-factory.md` Recover section documents the reclaim path.
