# Recovery

Runtime state lives under the configured `artifact_root/<run-id>/`:

- `state.json`: terminal status and stopped stage.
- `decision-ledger.jsonl`: bounded routing and decision events.
- `delivery-plan.json`: accepted spec and ticket DAG.
- `wayfinder-*.json`: route, map, and per-decision resolutions when Wayfinder ran.
- `routes/`: strict worker selections.
- `receipts/`: per-ticket acceptance and commit evidence.
- `reviews/`: independent axis reports and controller verdicts.
- `worktrees/`: integration and ticket checkouts.

The run id is deterministic for workflow, workspace, and goal text. Rerunning the same goal
reuses matching artifacts and worktrees where safe, and fails on conflicting tracker
content. Inspect before retrying a failed run. Never delete or force-reset preserved
worktrees to make a retry pass.

Common stable failures:

- `wayfinder_fog_remaining`: the map still cannot support a specification.
- `ticket_dag_stalled`: no open ticket has all blockers closed.
- `ticket_receipt_*`: a worker did not prove its commit or acceptance criteria.
- `ticket_merge_failed`: parallel slices conflict during integration.
- `review_repair_rounds_exhausted`: accepted must-fix findings remain after the repair bound.
- `principal_proxy_*`: the controller exhausted its answer loop or escalated a protected
  category.
