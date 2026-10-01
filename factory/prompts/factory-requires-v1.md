# Work item: ordered factory states via `requires`

Ordered factory states need an explicit contract, not just TOML file
order plus FIFO claims. Items may declare `requires = [dedupe_key, ...]`;
the loader rejects unknown references, duplicates, and cycles, and intake
only enqueues an item once every required dedupe key is `succeeded` in
the durable queue. Gated items are reported under `waiting` so operators
see exactly which requirement is unmet.

## Acceptance criteria

- `requires` entries must reference declared items; duplicates, unknown
  keys, and cycles fail validation with `factory_requires_*` codes.
- `intake` skips gated items and reports them under `waiting`; after the
  requirement succeeds, the next intake enqueues the item.
- `validate`/`status` expose `requires` per item.
- Tests cover gating, ungating, and all rejection cases.
