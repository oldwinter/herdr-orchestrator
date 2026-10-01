# Work item: bound per-item evidence files

Every attempt writes an evidence JSON under
`.orchestrator/factory/evidence/<dedupe_key>/`; across retries and long
sessions the directory grows without bound. Keep only the newest
`EVIDENCE_KEEP_PER_ITEM` (25) files per item, pruning oldest-first after
each write.

## Acceptance criteria

- After any evidence write, at most 25 files remain per item.
- The newest file is always retained.
- `tests/test_devin_factory.py` covers the bound.
