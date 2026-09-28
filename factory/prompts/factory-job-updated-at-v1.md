Expose when each job last changed.

Contract:
- Store.jobs() projects created_at/updated_at (additive).
- `factory status` job rows include updated_at_utc as an ISO timestamp.
