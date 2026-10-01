# Work item: fail-fast checks and operator recovery recipes

Acceptance checks are ordered: once a check exits non-zero the item has
failed, and running later checks only spends attempt budget. Stop at the
first failure and record how many checks were skipped in the evidence.

Operators also need recipes for the factory queue's recovery lifecycle,
not raw CLI invocations: `factory-retry JOB_ID` re-queues a terminal
failure with extra attempts and `factory-gc` collects terminal artifacts.

## Acceptance criteria

- After a non-zero check, no later check executes; evidence records
  `checks` (executed) and `checks_skipped`.
- The durable outcome is still `factory_check_failed` /
  `factory_check_timeout` with no success recorded.
- `just factory-retry` and `just factory-gc` exist and target the
  devin-factory workflow.
- `tests/test_devin_factory.py` covers the short-circuit path.
