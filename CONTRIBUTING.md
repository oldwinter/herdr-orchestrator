# Contributing

Read [`AGENTS.md`](AGENTS.md) for repository semantics and safety boundaries, then use the locked
development environment documented in [`docs/development.md`](docs/development.md).

1. Run `uv sync --locked`.
2. Add focused behavioral tests before or with the implementation.
3. Run `just lint` and the smallest relevant pytest target while iterating.
4. Run `just check` before requesting review.
5. Link the issue, explain risk and rollback, and use the pull request template.

User-facing CLI changes must regenerate [`docs/generated/cli.md`](docs/generated/cli.md) with
`just docs-generate`. Changes to telemetry or exporters must update
[`docs/observability.md`](docs/observability.md), `.env.example`, and the feature flag lifecycle.
Never commit runtime state, prompts, terminal output, credentials, or real `.env` values.

Security vulnerabilities must use the private process in [`SECURITY.md`](SECURITY.md), not a
public issue.
