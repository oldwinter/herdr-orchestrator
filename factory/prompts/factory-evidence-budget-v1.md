Make evidence self-documenting about the enforced dispatch budget.

Contract:
- Every evidence record includes `budget_seconds` (the agent_timeout_seconds
  the dispatch ran under) and `elapsed_seconds` (wall time of the dispatch),
  so an auditor can prove checks ran inside their deadline without replaying.
