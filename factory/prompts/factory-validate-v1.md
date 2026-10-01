# Work item: dry-run validate verb

Give operators a zero-mutation way to check a factory backlog before
intake. `devin_factory.py validate` parses and validates the backlog,
summarizes each item (dedupe key, harness support against the workflow's
workers, declared checks, receipt path), and reports which items are
already queued by opening the state DB read-only when it exists.

## Acceptance criteria

- `python3 scripts/devin_factory.py validate` exits 0 on a valid backlog
  and prints a JSON summary with one entry per item.
- An invalid backlog exits non-zero with a stable `factory_*` error code.
- `validate` never creates or writes the state DB.
- `tests/test_devin_factory.py` covers valid output, absent state DB, and
  invalid input.
