from __future__ import annotations

import json
import unittest

from herdr_orchestrator.completion import (
    CompletionIdentity,
    CompletionPolicy,
    CompletionResult,
    CompletionStatus,
    VerificationClass,
    parse_structured_completion,
)


class CompletionProtocolTests(unittest.TestCase):
    def test_parses_one_current_attempt_envelope(self) -> None:
        identity = CompletionIdentity(job_id=41, attempt=2, fencing_token="fence-current")
        output = (
            "Work finished.\n"
            'HERDR-COMPLETION-V2 {"schema_version":2,"job_id":41,"attempt":2,'
            '"fencing_token":"fence-current","status":"completed",'
            '"evidence_summary":"Focused tests passed"}\n'
        )

        result = parse_structured_completion("", output, identity)

        self.assertEqual(
            result,
            CompletionResult(
                policy=CompletionPolicy.STRUCTURED_V2,
                verification=VerificationClass.VERIFIED,
                status=CompletionStatus.COMPLETED,
                evidence_summary="Focused tests passed",
                error_code=None,
            ),
        )

    def test_rejects_adversarial_structured_envelopes(self) -> None:
        identity = CompletionIdentity(job_id=41, attempt=2, fencing_token="fence-current")
        valid = _envelope()
        cases = {
            "missing": ("", "ordinary output", "completion_envelope_missing"),
            "stale": (valid, valid, "completion_envelope_stale"),
            "malformed": ("", "HERDR-COMPLETION-V2 {", "completion_envelope_malformed"),
            "duplicate": ("", f"{valid}\n{valid}", "completion_envelope_duplicate"),
            "oversized output": (
                "",
                f"{'x' * 32_769}\n{valid}",
                "completion_output_oversized",
            ),
            "wrong schema": ("", _envelope(schema_version=1), "completion_schema_mismatch"),
            "wrong job": ("", _envelope(job_id=42), "completion_job_mismatch"),
            "wrong attempt": ("", _envelope(attempt=1), "completion_attempt_mismatch"),
            "wrong token": (
                "",
                _envelope(fencing_token="fence-old"),
                "completion_fencing_token_mismatch",
            ),
            "invalid status": (
                "",
                _envelope(status="unknown"),
                "completion_status_invalid",
            ),
            "oversized evidence": (
                "",
                _envelope(evidence_summary="x" * 1_001),
                "completion_evidence_oversized",
            ),
            "empty evidence": (
                "",
                _envelope(evidence_summary=""),
                "completion_evidence_invalid",
            ),
            "boolean job": (
                "",
                _envelope(job_id=True),
                "completion_envelope_invalid",
            ),
            "unknown field": (
                "",
                _envelope(extra="value"),
                "completion_envelope_invalid",
            ),
            "duplicate field": (
                "",
                (
                    'HERDR-COMPLETION-V2 {"schema_version":2,"job_id":41,"job_id":41,'
                    '"attempt":2,"fencing_token":"fence-current","status":"completed",'
                    '"evidence_summary":"tests passed"}'
                ),
                "completion_envelope_invalid",
            ),
        }

        for label, (before, after, error_code) in cases.items():
            with self.subTest(label=label):
                result = parse_structured_completion(before, after, identity)
                self.assertEqual(result.verification, VerificationClass.VERIFICATION_FAILED)
                self.assertEqual(result.error_code, error_code)
                self.assertIsNone(result.status)
                self.assertIsNone(result.evidence_summary)

    def test_accepts_all_declared_statuses_and_sanitizes_evidence(self) -> None:
        identity = CompletionIdentity(job_id=41, attempt=2, fencing_token="fence-current")

        for status in CompletionStatus:
            with self.subTest(status=status.value):
                secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz"
                result = parse_structured_completion(
                    "",
                    _envelope(
                        status=status.value,
                        evidence_summary=f"tests passed token={secret}",
                    ),
                    identity,
                )
                self.assertEqual(result.verification, VerificationClass.VERIFIED)
                self.assertEqual(result.status, status)
                self.assertEqual(result.evidence_summary, "tests passed token=[REDACTED]")


def _envelope(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_version": 2,
        "job_id": 41,
        "attempt": 2,
        "fencing_token": "fence-current",
        "status": "completed",
        "evidence_summary": "Focused tests passed",
    }
    payload.update(overrides)
    return "HERDR-COMPLETION-V2 " + json.dumps(payload, separators=(",", ":"))


if __name__ == "__main__":
    unittest.main()
