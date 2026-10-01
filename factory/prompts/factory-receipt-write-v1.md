# Work item: receipt write failure must be a stable factory error

An unwritable receipt target previously propagated `OSError` out of the
dispatcher and surfaced as `dispatcher_unhandled_error`. It now returns a
`verification-failed` completion with stable code
`factory_receipt_write_failed`, so the durable outcome names the real
cause and never records success. The lane is also documented in
`docs/architecture.md` alongside the other queue modes.

## Acceptance criteria

- An unwritable receipt directory fails the job with
  `error_code = factory_receipt_write_failed`, `task_verified = false`.
- `docs/architecture.md` describes the lane and its queue reuse.
- Tests cover the fail-closed path.
