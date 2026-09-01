from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from herdr_orchestrator.feature_flags import FeatureFlag, FeatureFlagError, enabled
from herdr_orchestrator.observability import MAX_TEXT, Observability, sanitize


class FeatureFlagTests(unittest.TestCase):
    def test_flags_are_disabled_by_default_and_fail_closed(self) -> None:
        for flag in FeatureFlag:
            with self.subTest(flag=flag):
                self.assertFalse(enabled(flag, {}))
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
        private_path = "/srv/private/customer-alpha/job.txt"
        pane_id = "w9:p7"
        prompt = "[TEST-PROMPT] keep this private"
        sanitized = sanitize(
            {
                "token": "secret-value",
                "api_key": "api-secret",  # pragma: allowlist secret
                "message": "Authorization: [TEST-CREDENTIAL]",
                "nested": {
                    "password": "unsafe",  # pragma: allowlist secret
                    "items": [{"api_key": "nested-api-secret"}],  # pragma: allowlist secret
                },
                "raw_prompt": prompt,
                "execution_path": private_path,
                "pane_id": pane_id,
                "summary": f'prompt="{prompt}" failed at {private_path} pane {pane_id}',
                "opaque": Path(private_path),
                "long_summary": "x" * (MAX_TEXT + 50),
                "panel": "visible",
                "empathy": "visible",
            }
        )
        assert isinstance(sanitized, dict)
        self.assertEqual(sanitized["token"], "[REDACTED]")
        self.assertEqual(sanitized["api_key"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["password"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["items"][0]["api_key"], "[REDACTED]")
        self.assertNotIn("[TEST-CREDENTIAL]", sanitized["message"])
        self.assertEqual(sanitized["raw_prompt"], "[REDACTED]")
        self.assertEqual(sanitized["execution_path"], "[REDACTED]")
        self.assertEqual(sanitized["pane_id"], "[REDACTED]")
        serialized = json.dumps(sanitized)
        self.assertNotIn(prompt, serialized)
        self.assertNotIn(private_path, serialized)
        self.assertNotIn(pane_id, serialized)
        self.assertEqual(len(sanitized["long_summary"]), MAX_TEXT)
        self.assertEqual(sanitized["panel"], "visible")
        self.assertEqual(sanitized["empathy"], "visible")

    def test_local_events_and_metrics_include_correlation_without_prompts(self) -> None:
        private_path = "/srv/private/customer-alpha/job.txt"
        pane_id = "w9:p7"
        prompt = "[TEST-PROMPT] keep this private"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = Observability(root, "example", environ={}, clock=lambda: 42.0)
            telemetry.event(
                "dispatch_started",
                correlation_id="trace-1",
                fields={
                    "prompt": prompt,
                    "harness": "pi",
                    "execution_path": private_path,
                    "pane_id": pane_id,
                    "summary": f'prompt="{prompt}" at {private_path} pane {pane_id}',
                },
            )
            telemetry.metric(
                "dispatch_duration_seconds",
                1.25,
                correlation_id="trace-1",
            )
            telemetry.alert("dispatch_needs_attention", correlation_id="trace-1")

            event = json.loads((root / "events.jsonl").read_text())
            metric = json.loads((root / "metrics.jsonl").read_text())
            alert = json.loads((root / "alerts.jsonl").read_text())

        self.assertEqual(event["correlation_id"], "trace-1")
        self.assertEqual(event["fields"]["prompt"], "[REDACTED]")
        self.assertEqual(metric["value"], 1.25)
        self.assertEqual(
            set(event),
            {"correlation_id", "event", "fields", "observed_at", "schema_version", "workflow"},
        )
        self.assertEqual(
            set(metric),
            {"correlation_id", "event", "observed_at", "schema_version", "value", "workflow"},
        )
        self.assertEqual(
            set(alert),
            {"correlation_id", "event", "observed_at", "schema_version", "workflow"},
        )
        serialized = json.dumps(event)
        self.assertNotIn(prompt, serialized)
        self.assertNotIn(private_path, serialized)
        self.assertNotIn(pane_id, serialized)

    def test_enabled_exporters_send_only_sanitized_https_payloads(self) -> None:
        private_path = "/srv/private/customer-alpha/job.txt"
        pane_id = "w9:p7"
        prompt = "[TEST-PROMPT] keep this private"
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
            telemetry = Observability(
                Path(temporary),
                f"example at {private_path}",
                environ=environ,
            )
            with patch("herdr_orchestrator.observability.urllib.request.urlopen", opener):
                telemetry.event(
                    f'dispatch_finished prompt="{prompt}"',
                    correlation_id=f"trace-2 pane_id={pane_id}",
                    fields={
                        "password": "[TEST-PASSWORD]",  # pragma: allowlist secret
                        "summary": f'prompt="{prompt}" at {private_path} pane {pane_id}',
                        "nested": {"api_key": "nested-export-secret"},  # pragma: allowlist secret
                    },
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
        self.assertTrue(all(prompt.encode() not in request.data for request in requests))
        self.assertTrue(all(private_path.encode() not in request.data for request in requests))
        self.assertTrue(all(pane_id.encode() not in request.data for request in requests))
        self.assertTrue(all(b"nested-export-secret" not in request.data for request in requests))

    def test_local_persistence_sanitizes_the_entire_record(self) -> None:
        private_path = "/srv/private/customer-alpha/job.txt"
        pane_id = "w9:p7"
        prompt = "[TEST-PROMPT] keep this private"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = Observability(
                root,
                f"workflow at {private_path}",
                environ={},
                clock=lambda: 42.0,
            )
            telemetry.event(
                f'dispatch_started prompt="{prompt}" at {private_path}',
                correlation_id=f"trace-1 pane_id={pane_id}",
                fields={
                    "nested": [{"api_key": "nested-api-secret"}],  # pragma: allowlist secret
                    "visible": "keep this correlation context",
                },
            )
            telemetry.metric(
                "dispatch_duration token=secret-value",
                1.25,
                correlation_id=f"trace-1 at {private_path}",
                fields={"nested": {"api_key": "metric-api-secret"}},  # pragma: allowlist secret
            )

            event = json.loads((root / "events.jsonl").read_text())
            metric = json.loads((root / "metrics.jsonl").read_text())

        for record in (event, metric):
            serialized = json.dumps(record)
            self.assertNotIn(prompt, serialized)
            self.assertNotIn(private_path, serialized)
            self.assertNotIn(pane_id, serialized)
            self.assertNotIn("nested-api-secret", serialized)
            self.assertNotIn("metric-api-secret", serialized)
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["correlation_id"], "trace-1 pane_id=[REDACTED]")
        self.assertEqual(event["fields"]["nested"][0]["api_key"], "[REDACTED]")
        self.assertEqual(event["fields"]["visible"], "keep this correlation context")
        self.assertEqual(metric["value"], 1.25)

    def test_malformed_https_exporter_endpoint_fails_soft(self) -> None:
        environ = {
            "HERDR_FEATURE_WEBHOOK_ALERTS": "true",
            "HERDR_ALERT_WEBHOOK_URL": "https://[",
        }
        opener = MagicMock()
        with tempfile.TemporaryDirectory() as temporary:
            telemetry = Observability(Path(temporary), "example", environ=environ)
            with patch("herdr_orchestrator.observability.urllib.request.urlopen", opener):
                telemetry.alert("dispatch_needs_attention", correlation_id="trace-4")
        opener.assert_not_called()

    def test_exporter_serialization_failure_fails_soft(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            telemetry = Observability(Path(temporary), "example", environ={})
            with patch(
                "herdr_orchestrator.observability.json.dumps",
                side_effect=ValueError("cannot serialize"),
            ):
                telemetry._post_json("https://alerts.example.test/herdr", {"event": "test"})

    def test_exporter_transport_failure_fails_soft_with_bounded_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            telemetry = Observability(Path(temporary), "example", environ={})
            opener = MagicMock(side_effect=RuntimeError("transport unavailable"))
            with patch("herdr_orchestrator.observability.urllib.request.urlopen", opener):
                telemetry._post_json("https://alerts.example.test/herdr", {"event": "test"})
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(opener.call_args.kwargs["timeout"], 2)

    def test_invalid_exporter_flag_fails_closed_inside_telemetry(self) -> None:
        environ = {
            "HERDR_FEATURE_SENTRY_EXPORT": "maybe",
            "SENTRY_DSN": "https://public@example.test/project",  # pragma: allowlist secret
        }
        opener = MagicMock()
        with tempfile.TemporaryDirectory() as temporary:
            telemetry = Observability(Path(temporary), "example", environ=environ)
            with patch("herdr_orchestrator.observability.urllib.request.urlopen", opener):
                telemetry.event("dispatch_finished", correlation_id="trace-5")
        opener.assert_not_called()

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
        assert isinstance(sanitized, list)
        self.assertEqual(sanitized[:4], [None, True, 1, 1.5])
        self.assertIsInstance(sanitized[4], str)


if __name__ == "__main__":
    unittest.main()
