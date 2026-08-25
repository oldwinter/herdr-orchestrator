# Security policy

## Supported versions

The latest npm release and the current `main` branch receive security fixes.

## Reporting

Use GitHub private vulnerability reporting:
<https://github.com/oldwinter/herdr-orchestrator/security/advisories/new>.
Do not open a public issue with credentials, prompts, terminal output, or exploit details.

## Security controls

- `just security` scans source, dependencies, and tracked content.
- CI uploads machine-readable findings and opens one deduplicated insight issue when the
  default branch security gate fails.
- Dependencies and GitHub Actions are exact-pinned or lockfile-pinned. Dependabot waits seven
  days before proposing new releases.
- Runtime telemetry is local by default, redacted before persistence/export, and externally
  exported only behind fail-closed feature flags.

See [`docs/observability.md`](docs/observability.md) for data handling and incident response.
