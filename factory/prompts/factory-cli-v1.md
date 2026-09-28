# Work item: CLI-level end-to-end coverage

The factory tests exercised functions in-process but nothing covered the
real entry point — argv parsing, exit codes, and JSON on stdout/stderr.
`FactoryCliTests` drives `scripts/devin_factory.py` as a subprocess
through intake, run, status, and report on a fixture workflow, asserts
`run` exits 1 when the queue ends with terminal failures, and proves a
malformed backlog or missing workflow exits 2 with a stable error code.

## Acceptance criteria

- `intake`, `run`, `status`, and `report` produce JSON/report output with
  exit 0 on a healthy pipeline.
- `run` exits 1 when the queue contains a terminal failure.
- `validate` exits 2 with a `factory_*` error on invalid input.
