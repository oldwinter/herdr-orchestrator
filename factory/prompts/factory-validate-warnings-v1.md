Surface check timeouts that can never be honored.

Contract:
- `factory validate` output includes a `warnings` list naming every check
  whose timeout_seconds exceeds the workflow's agent_timeout_seconds,
  explaining the dispatch deadline will truncate it.
- Warnings are non-fatal: `valid` and exit code stay unchanged.
