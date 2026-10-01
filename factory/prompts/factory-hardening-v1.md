# Work item: validate hardening and status error summaries

`validate` opened the state DB with a raw sqlite query; a corrupt or
schema-less file raised an uncaught `OperationalError` (traceback, exit
1) instead of a clean report. It now degrades to `state_db_error` with
`queued` unknown. `status` also surfaced only `error_code`; it now
includes the bounded `error_summary` so operators see the failing check
without opening evidence files. An empty queue `run` is a clean idle
exit 0.

## Acceptance criteria

- `validate` exits 0 on a garbage state DB and reports
  `state_db_error` instead of crashing.
- `status` job rows carry `error_summary` alongside `error_code`.
- `run` on an empty queue exits 0 with `idle: true`, `claimed: 0`.
