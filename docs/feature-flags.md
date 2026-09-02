# Feature flag lifecycle

Feature flags are typed `FeatureFlag` members with an explicit environment-variable mapping.
They default to `false` and accept only the documented boolean spellings. An unknown flag,
non-mapping environment override, non-string value, or invalid spelling raises `FeatureFlagError`
with a stable `feature_flag_*` code.

Each flag must:

1. have one owner and one purpose in this table;
2. be referenced by production code and covered by a fail-closed test;
3. include operator configuration in `.env.example` and `docs/observability.md`;
4. be removed from code, tests, examples, and docs in the same change when retired.

| Flag | Owner | Purpose | Introduced | Review by | Exit condition |
| --- | --- | --- | --- | --- | --- |
| `sentry_export` | `@oldwinter` | Export sanitized error events to Sentry | 2026-08-25 | 2026-11-25 | Remove if no operator enables Sentry |
| `posthog_analytics` | `@oldwinter` | Export sanitized lifecycle events to PostHog | 2026-08-25 | 2026-11-25 | Remove if no product analytics backend is configured |
| `webhook_alerts` | `@oldwinter` | Send sanitized attention alerts to an HTTPS webhook | 2026-08-25 | 2026-11-25 | Remove after a single supported alert backend is selected |

`scripts/check_feature_flags.py` fails when a declared flag has no production consumer, lifecycle
row, environment example, observability table entry, or test reference. It parses Python and
configuration structures, so comments and prose do not count as evidence. It also rejects unknown
Python references, duplicate lifecycle rows, invalid declaration mappings, non-false example
defaults, and lifecycle rows for removed flags.
