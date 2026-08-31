# Development setup and quality gates

## One-command setup

Install Python 3.12+, Node.js 20+, `uv` 0.12.7, and `just`, then run:

```bash
uv sync --locked
just check
```

CI runs Python 3.14.7 and Node.js 26.8.1. The devcontainer performs `uv sync --locked`
automatically and keeps Python 3.12 as the compatibility floor while using Node.js 26.
Runtime smoke tests still require Herdr and authenticated harness CLIs on the host.
Copy `.env.example` only when testing optional exporters. Never commit `.env` files.

## Fast feedback

```bash
just lint
just test
just test-coverage
just security
just docs-check
just profile-tests
```

`just check` is the merge gate. It enforces formatting, strict typing, naming, duplication,
dead code, cyclomatic complexity, import boundaries, unused dependencies, repository policy,
documentation freshness, 80% branch-aware coverage, three repeated test runs, security scans,
and package build metrics. `just profile-tests` writes standard-library `cProfile` data to
`.orchestrator/quality/tests.pstats` for local hot-path investigation.
Pre-commit runs the fast static gates, while pre-push runs coverage:

```bash
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type pre-push
```

CI uses the same lockfile and commands, uploads `.orchestrator/quality`, and posts the generated
quality summary on pull requests. Do not edit `docs/generated/cli.md`; run `just docs-generate`.
