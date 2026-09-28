# Work item: per-item retry budget

Every backlog item inherited the workflow `max_attempts`; a flaky-prone
or deliberately single-shot item could not declare its own budget.
`[[items]]` gains `max_attempts` (1–8), threaded through
`Coordinator.enqueue_prompt_file` into `NewJob`. Existing jobs keep the
budget they were enqueued with — the dedupe contract is unchanged.

## Acceptance criteria

- `max_attempts = 1` on an item yields a single attempt even when the
  workflow default is higher; the job fails terminally after one run.
- Values outside 1–8 or non-integer are rejected with
  `factory_max_attempts_invalid`.
- The runner signature stays backwards compatible (optional kwarg).
