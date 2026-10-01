# factory-retry-blocked-v1

Give operators a recovery path for `blocked` factory jobs.

The local lane has no Herdr pane, so the canonical `resume` flow can
never proceed (`blocked_pane_missing` / `dispatcher_resume_unsupported`)
and `retry_failed` rejects anything but `failed` — a blocked job was
permanently wedged. `Store.retry_failed` gains an opt-in
`allow_blocked` parameter (default off: canonical lanes keep
failed-only semantics because a blocked Herdr job may still own a live
session); the `retry` CLI exposes `--allow-blocked`; `factory-retry`
passes it so a blocked local job re-drives on a fresh attempt.

Acceptance:

- `tests/test_store.py` pins `blocked` rejected by default and accepted
  with `allow_blocked=True`.
- `tests/test_devin_factory.py` pins a blocked job retried then drained
  to `succeeded` with `task_verified`.
- `tests/test_cli.py` pins the flag parse and store forwarding.
- `docs/devin-factory.md` documents the blocked recovery path.
