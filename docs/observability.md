# Observability, privacy, and incident response

## Local-first signals

Each dispatch attempt receives a random correlation ID. The coordinator persists it with the
job and receipt, projects it to the dashboard, and uses it in structured records under
`.orchestrator/telemetry/`:

| File | Purpose |
| --- | --- |
| `events.jsonl` | Dispatch lifecycle and bounded context |
| `metrics.jsonl` | Dispatch duration and numeric measurements |
| `alerts.jsonl` | Blocked, failed, and transport-error attention signals |

Every record has `schema_version`, `workflow`, `event`, `observed_at`, and `correlation_id`.
Events and alerts can add sanitized `fields`. Metrics add a numeric `value` and can also add
sanitized `fields`.

Local write and exporter network failures never alter queue state. Files are local runtime state
and are ignored by Git. Prompts and terminal output are never telemetry fields.

## Data handling

All telemetry passes through central sanitization before local persistence or export. Keys
containing authorization, cookie, credential, password, prompt, secret, session, terminal, or
token, as well as path or pane keys, are replaced with `[REDACTED]`. Common token, assignment,
filesystem path, and Herdr pane ID shapes in text are scrubbed. Whitespace is normalized, and text
is bounded to 300 characters. The anonymous installation ID is a one-way, 16-character digest of
the resolved telemetry root's parent directory, which is `.orchestrator` by default.

The system does not intentionally collect names, email addresses, IP addresses, prompt content,
filesystem paths, pane identifiers, terminal transcripts, cookies, or credentials. Operators must
not add PII to telemetry fields. Local files follow the operator's filesystem retention and
deletion policies.

## Optional exporters

Every exporter is off by default and rejects malformed feature-flag values. Copy `.env.example`
to local secret storage, provide each required endpoint credential, then enable the flags that you
need:

| Flag | Required configuration | Destination |
| --- | --- | --- |
| `HERDR_FEATURE_SENTRY_EXPORT` | `SENTRY_DSN` | Sanitized lifecycle events tagged as errors |
| `HERDR_FEATURE_POSTHOG_ANALYTICS` | `POSTHOG_API_KEY`, optional HTTPS `POSTHOG_HOST` | Sanitized lifecycle events |
| `HERDR_FEATURE_WEBHOOK_ALERTS` | HTTPS `HERDR_ALERT_WEBHOOK_URL` | Sanitized alerts |

Never commit real values. Exporters use HTTPS, a two-second timeout, and fail closed when a
credential or endpoint is absent.

## Alert runbook

1. Use the correlation ID to join the alert, job, and receipt. Do not copy full terminal output.
2. Run `just status` and classify the stable error code using
   [`runtime-troubleshooting.md`](runtime-troubleshooting.md).
3. For a dependency or source finding, open the CI quality artifact and reproduce with
   `just security`.
4. Contain exporter incidents by setting all `HERDR_FEATURE_*` flags to `false`.
5. Rotate an exposed credential in its owning service, remove local runtime files if required,
   and record only sanitized evidence.
6. Fix with a focused regression test, run `just check`, and close the insight issue only after
   the default branch is green.

There is no automatic production deployment. npm releases are immutable; recover by fixing
forward and publishing a new version, then deprecating the affected version when necessary.
