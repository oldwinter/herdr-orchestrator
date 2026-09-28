Expose stale claims in factory status.

Contract:
- Store.jobs() projects lease_until (additive).
- `factory status` job rows include lease_expired=true when lease_until
  is in the past, marking the claim reclaimable on the next drain.
