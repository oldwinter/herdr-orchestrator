# Work item: status must surface backlog errors, not silence

`status` caught `FactoryError` and printed `items: 0`, making a broken
backlog indistinguishable from an empty one. It now reports the stable
error code under `backlog.error`. Also pin cross-process intake
idempotence: two concurrent `devin_factory.py intake` invocations must
produce exactly one job and `added` totals of 1, guarded by the durable
dedupe contract.

## Acceptance criteria

- `status` exits 0 and reports `backlog.error` when the backlog is
  invalid instead of claiming zero items.
- Two concurrent intake processes yield one job total.
- Tests cover both behaviors.
