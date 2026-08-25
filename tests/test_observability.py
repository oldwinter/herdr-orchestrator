from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from herdr_orchestrator.feature_flags import FeatureFlag, FeatureFlagError, enabled
from herdr_orchestrator.observability import Observability, sanitize


class FeatureFlagTests(unittest.TestCase):
    def test_flags_are_disabled_by_default_and_fail_closed(self) -> None:
        self.assertFalse(enabled(FeatureFlag.SENTRY_EXPORT, {}))
        with self.assertRaisesRegex(FeatureFlagError, "feature_flag_invalid"):
            enabled(FeatureFlag.SENTRY_EXPORT, {"HERDR_FEATURE_SENTRY_EXPORT": "maybe"})

    def test_explicit_true_enables_known_flag(self) -> None:
        self.assertTrue(
            enabled(
                FeatureFlag.POSTHOG_ANALYTICS,
                {"HERDR_FEATURE_POSTHOG_ANALYTICS": "true"},
            )
        )

    def test_webhook_alert_flag_is_typed_and_disabled_by_default(self) -> None:
        self.assertFalse(enabled(FeatureFlag.WEBHOOK_ALERTS, {}))


class ObservabilityTests(unittest.TestCase):
    def test_sanitizer_redacts_keys_and_common_token_shapes(self) -> None:
        sanitized = sanitize(
            {
                "token": "secret-value",
                "message": "Authorization: [TEST-CREDENTIAL]",
                "nested": {"password": "unsafe"},  # pragma: allowlist secret
            }
        )
        self.assertEqual(sanitized["token"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["password"], "[REDACTED]")
        self.assertNotIn("[TEST-CREDENTIAL]", sanitized["message"])

    def test_local_events_and_metrics_include_correlation_without_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = Observability(root, "example", environ={}, clock=lambda: 42.0)
            telemetry.event(
                "dispatch_started",
                correlation_id="trace-1",
                fields={"prompt": "private", "harness": "pi"},
            )
            telemetry.metric(
                "dispatch_duration_seconds",
                1.25,
                correlation_id="trace-1",
            )

            event = json.loads((root / "events.jsonl").read_text())
            metric = json.loads((root / "metrics.jsonl").read_text())

        self.assertEqual(event["correlation_id"], "trace-1")
        self.assertEqual(event["fields"]["prompt"], "[REDACTED]")
        self.assertEqual(metric["value"], 1.25)

    def test_enabled_exporters_send_only_sanitized_https_payloads(self) -> None:
        environ = {
            "HERDR_ALERT_WEBHOOK_URL": "https://alerts.example.test/herdr",
            "HERDR_FEATURE_POSTHOG_ANALYTICS": "true",
            "HERDR_FEATURE_SENTRY_EXPORT": "true",
            "HERDR_FEATURE_WEBHOOK_ALERTS": "true",
            "POSTHOG_API_KEY": "public-project-key",  # pragma: allowlist secret
            "POSTHOG_HOST": "https://analytics.example.test",
            "SENTRY_DSN": "https://public@example.test/project",  # pragma: allowlist secret
        }
        opener = MagicMock()
        opener.return_value.__enter__.return_value = object()
        with tempfile.TemporaryDirectory() as temporary:
            telemetry = Observability(Path(temporary), "example", environ=environ)
            with patch("herdr_orchestrator.observability.urllib.request.urlopen", opener):
                telemetry.event(
                    "dispatch_finished",
                    correlation_id="trace-2",
                    fields={"password": "[TEST-PASSWORD]"},  # pragma: allowlist secret
                )
                telemetry.alert(
                    "dispatch_needs_attention",
                    correlation_id="trace-2",
                    fields={"token": "do-not-send"},  # pragma: allowlist secret
                )

        self.assertEqual(opener.call_count, 3)
        requests = [call.args[0] for call in opener.call_args_list]
        self.assertTrue(all(request.full_url.startswith("https://") for request in requests))
        self.assertTrue(all(b"[TEST-PASSWORD]" not in request.data for request in requests))

    def test_exporters_fail_closed_for_missing_or_invalid_configuration(self) -> None:
        environ = {
            "HERDR_ALERT_WEBHOOK_URL": "http://localhost/unsafe",
            "HERDR_FEATURE_POSTHOG_ANALYTICS": "true",
            "HERDR_FEATURE_SENTRY_EXPORT": "true",
            "HERDR_FEATURE_WEBHOOK_ALERTS": "true",
            "POSTHOG_HOST": "http://localhost/unsafe",
            "SENTRY_DSN": "invalid",
        }
        opener = MagicMock()
        with tempfile.TemporaryDirectory() as temporary:
            telemetry = Observability(Path(temporary), "example", environ=environ)
            with patch("herdr_orchestrator.observability.urllib.request.urlopen", opener):
                telemetry.event("dispatch_finished", correlation_id="trace-3")
                telemetry.alert("dispatch_needs_attention", correlation_id="trace-3")
        opener.assert_not_called()

    def test_sanitizer_handles_sequences_scalars_and_unknown_objects(self) -> None:
        sanitized = sanitize([None, True, 1, 1.5, object()])
        self.assertEqual(sanitized[:4], [None, True, 1, 1.5])
        self.assertIsInstance(sanitized[4], str)


if __name__ == "__main__":
    unittest.main()
