Make factory run output actionable without a follow-up status call.

Contract:
- `factory run` (both until-idle and --once) includes a `jobs` list:
  job_id, dedupe_key, state, task_verified, error_code for every queued job,
  so a failed item is identifiable directly from the run output.
