Document the run output contract added by factory-run-jobs-v1.

Contract:
- docs/devin-factory.md describes the `jobs` list in run JSON output
  (job_id, dedupe_key, state, task_verified, error_code).
- It also states that the drain deadline truncates in-flight dispatches
  via dispatch_deadline.
- scripts/check_docs.py stays green.
