# Factory scaffold v1

Build the local software-factory lane for this repository on top of the
canonical durable queue: a committed backlog with acceptance criteria, a
dedicated `devin-factory` workflow, an idempotent intake, a local dispatcher
that runs declared checks, timestamped evidence, and an operator report.

## Acceptance criteria

- `python3 -m pytest tests/test_devin_factory.py -q` passes: intake
  idempotence, dedupe-contract drift, successful item end to end, failed
  check propagation without success, unknown-item fail closed, and
  retry-after-fix recovery.
