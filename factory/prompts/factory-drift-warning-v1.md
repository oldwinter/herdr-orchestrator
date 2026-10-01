# factory-drift-warning-v1

Warn early when a queued item's durable contract drifts from the backlog.

Editing `title`, `harness`, `prompt_file` content or `receipt` on an
already-queued item makes the next intake fail with
`dedupe_contract_conflict`. `factory-validate` now compares those fields
against the durable job rows (read-only) and emits a warning naming the
drifted fields, so operators see the problem before the intake pass
breaks. Fields outside the contract (`checks`, `check_timeout_seconds`,
`requires`, `max_attempts`) are documented as live-at-dispatch,
intake-gate-only, or pinned-at-enqueue respectively.

Acceptance:

- `tests/test_devin_factory.py` pins a queued item whose title+prompt
  drift produces the dedupe_contract_conflict warning.
- `docs/devin-factory.md` documents which fields form the contract and
  how each editable field behaves after queueing.
