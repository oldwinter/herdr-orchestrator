from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.delivery_protocol import (
    AuthorityCategory,
    DeliveryArtifactError,
    DeliveryTicket,
    ProxyAction,
    load_delivery_plan,
    load_proxy_decision,
    load_review_verdict,
    load_ticket_receipt,
)


class DeliveryProtocolTests(unittest.TestCase):
    def test_loads_dependency_ordered_tracer_bullet_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(json.dumps(_plan()), encoding="utf-8")

            plan = load_delivery_plan(path)

        self.assertEqual(plan.slug, "focused-delivery")
        self.assertEqual(plan.tickets[1].blocked_by, ("01",))
        self.assertEqual(plan.seams, ("CLI output",))

    def test_rejects_ticket_that_precedes_its_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            payload = _plan()
            payload["tickets"][0]["blocked_by"] = ["02"]
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                DeliveryArtifactError,
                "ticket_blocker_must_precede",
            ):
                load_delivery_plan(path)

    def test_proxy_requires_secret_and_production_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decision.json"
            path.write_text(
                json.dumps(
                    {
                        "action": "approve",
                        "category": "secret",
                        "response": "yes",
                        "rationale": "needed",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DeliveryArtifactError,
                "must_escalate",
            ):
                load_proxy_decision(path)

            path.write_text(
                json.dumps(
                    {
                        "action": "escalate",
                        "category": "production",
                        "response": "",
                        "rationale": "production boundary",
                    }
                ),
                encoding="utf-8",
            )
            decision = load_proxy_decision(path)

        self.assertEqual(decision.action, ProxyAction.ESCALATE)
        self.assertEqual(decision.category, AuthorityCategory.PRODUCTION)

    def test_review_verdict_must_account_for_every_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "verdict.json"
            path.write_text(
                json.dumps(
                    {
                        "accepted": ["standards:1"],
                        "dismissed": [],
                        "rationale": "one citation is valid",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DeliveryArtifactError,
                "incomplete",
            ):
                load_review_verdict(
                    path,
                    candidates=("standards:1", "spec:1"),
                )

    def test_ticket_receipt_requires_full_commit_sha(self) -> None:
        ticket = DeliveryTicket(
            ticket_id="01",
            title="Implement the behavior",
            what_to_build="Add the accepted behavior.",
            blocked_by=(),
            acceptance_criteria=("The behavior works.",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            payload = _receipt()
            path.write_text(json.dumps(payload), encoding="utf-8")

            receipt = load_ticket_receipt(path, ticket)

            self.assertEqual(receipt.commit, "a" * 40)
            payload["commit"] = "abcdef1"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                DeliveryArtifactError,
                "ticket_receipt_commit_invalid",
            ):
                load_ticket_receipt(path, ticket)

    def test_review_verdict_rejects_duplicate_finding_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "verdict.json"
            path.write_text(
                json.dumps(
                    {
                        "accepted": ["standards:1", "standards:1"],
                        "dismissed": [],
                        "rationale": "the finding is valid",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DeliveryArtifactError,
                "review_verdict_duplicate",
            ):
                load_review_verdict(path, candidates=("standards:1",))

    def test_rejects_duplicate_json_object_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "verdict.json"
            path.write_text(
                '{"accepted":[],"accepted":["standards:1"],'
                '"dismissed":[],"rationale":"valid"}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DeliveryArtifactError,
                "review_verdict_duplicate_key",
            ):
                load_review_verdict(path, candidates=("standards:1",))

    def test_invalid_utf8_is_reported_as_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "verdict.json"
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(
                DeliveryArtifactError,
                "review_verdict_invalid_json",
            ):
                load_review_verdict(path, candidates=())

    def test_missing_ticket_receipt_is_rejected(self) -> None:
        ticket = DeliveryTicket(
            ticket_id="01",
            title="Implement the behavior",
            what_to_build="Add the accepted behavior.",
            blocked_by=(),
            acceptance_criteria=("The behavior works.",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.json"

            with self.assertRaisesRegex(
                DeliveryArtifactError,
                "ticket_receipt_missing",
            ):
                load_ticket_receipt(path, ticket)


def _plan() -> dict[str, object]:
    return {
        "slug": "focused-delivery",
        "title": "Focused delivery",
        "problem_statement": "The behavior is missing.",
        "solution": "Add the behavior.",
        "user_stories": ["As a user, I can run it."],
        "implementation_decisions": ["Use the existing module."],
        "testing_decisions": ["Test public behavior."],
        "out_of_scope": [],
        "further_notes": [],
        "seams": ["CLI output"],
        "tickets": [
            {
                "id": "01",
                "title": "Add the first slice",
                "what_to_build": "Expose one complete behavior.",
                "blocked_by": [],
                "acceptance_criteria": ["The first behavior works."],
            },
            {
                "id": "02",
                "title": "Add the second slice",
                "what_to_build": "Expose the dependent behavior.",
                "blocked_by": ["01"],
                "acceptance_criteria": ["The second behavior works."],
            },
        ],
    }


def _receipt() -> dict[str, object]:
    return {
        "ticket_id": "01",
        "commit": "a" * 40,
        "acceptance": [
            {
                "criterion": "The behavior works.",
                "passed": True,
                "evidence": "The focused test passed.",
            }
        ],
        "checks": ["pytest passed"],
        "summary": "Implemented the behavior.",
    }


if __name__ == "__main__":
    unittest.main()
