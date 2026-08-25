# Feature flag lifecycle

Feature flags are typed `FeatureFlag` members with an explicit environment-variable mapping.
They default to false, accept only documented boolean spellings, and raise a stable error for
invalid values.

Each flag must:

1. have one owner and one purpose in this table;
2. be referenced by production code and covered by a fail-closed test;
3. include operator configuration in `.env.example` and `docs/observability.md`;
4. be removed from code, tests, examples, and docs in the same change when retired.

| Flag | Owner | Introduced | Review by | Exit condition |
| --- | --- | --- | --- | --- |
| `sentry_export` | `@oldwinter` | 2026-08-25 | 2026-11-25 | Remove if no operator enables Sentry |
| `posthog_analytics` | `@oldwinter` | 2026-08-25 | 2026-11-25 | Remove if no product analytics backend is configured |
| `webhook_alerts` | `@oldwinter` | 2026-08-25 | 2026-11-25 | Remove after a single supported alert backend is selected |

`scripts/check_feature_flags.py` fails when a declared flag has no production consumer, lifecycle
row, environment example, or test reference. It also rejects lifecycle rows for removed flags.
