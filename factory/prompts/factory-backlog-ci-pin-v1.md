Keep the committed factory backlog honest in CI.

Contract:
- A regression test runs `factory validate` against the repo's real
  workflows/devin-factory.toml + factory/backlog.toml and asserts the
  payload is valid, has items, no unsupported harnesses, no warnings.
