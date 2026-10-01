Explain why a pending job is not being claimed.

Contract:
- Store.jobs() projects available_at.
- `factory status` pending rows include retry_backoff_seconds — the
  remaining delay before the job becomes claimable.
