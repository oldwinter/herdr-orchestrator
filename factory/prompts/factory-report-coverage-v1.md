Enrich the factory operator report with backlog coverage.

Contract:
- `factory report` keeps the jobs table and adds a Backlog coverage section:
  total items, queued count, unqueued dedupe keys, and items waiting on
  `requires` with their blockers named.
- A broken backlog must not break the report — render an error line instead.
