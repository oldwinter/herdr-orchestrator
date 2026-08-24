from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.delivery_protocol import (
    AcceptanceResult,
    DeliveryPlan,
    DeliveryTicket,
    TicketReceipt,
)
from herdr_orchestrator.tracker import GithubTracker, LocalMarkdownTracker, TrackerError


class LocalMarkdownTrackerTests(unittest.TestCase):
    def test_publishes_one_file_per_ticket_and_closes_with_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracker = LocalMarkdownTracker(root)
            plan = _plan()

            references = tracker.publish(plan)
            tracker.close(
                plan.tickets[0],
                TicketReceipt(
                    ticket_id="01",
                    commit="abcdef1234567890",
                    acceptance=(
                        AcceptanceResult("The behavior works.", True, "test passes"),
                    ),
                    checks=("python -m unittest: passed",),
                    summary="Implemented the slice.",
                ),
            )
            resumed = LocalMarkdownTracker(root)
            resumed_references = resumed.publish(plan)

            issue = Path(references["01"].reference).read_text(encoding="utf-8")

        self.assertIn("**Status:** completed", issue)
        self.assertIn("- [x] The behavior works.", issue)
        self.assertIn("`abcdef1234567890`", issue)
        self.assertEqual(resumed_references, references)

    def test_refuses_to_overwrite_conflicting_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracker = LocalMarkdownTracker(root)
            plan = _plan()
            tracker.publish(plan)
            spec = root / plan.slug / "spec.md"
            spec.write_text("user work", encoding="utf-8")

            with self.assertRaisesRegex(TrackerError, "artifact_conflict"):
                tracker.publish(plan)

    def test_refuses_to_close_over_concurrent_ticket_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracker = LocalMarkdownTracker(root)
            plan = _plan()
            references = tracker.publish(plan)
            path = Path(references["01"].reference)
            path.write_text(
                path.read_text(encoding="utf-8") + "\nHuman note.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(TrackerError, "artifact_conflict"):
                tracker.close(
                    plan.tickets[0],
                    TicketReceipt(
                        ticket_id="01",
                        commit="abcdef1234567890",
                        acceptance=(
                            AcceptanceResult(
                                "The behavior works.",
                                True,
                                "test passes",
                            ),
                        ),
                        checks=("python -m unittest: passed",),
                        summary="Implemented the slice.",
                    ),
                )


class FakeGithubRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.issue_number = 40

    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        stdout = ""
        if argv[1:3] == ["issue", "create"]:
            self.issue_number += 1
            stdout = f"https://github.com/owner/project/issues/{self.issue_number}\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class GithubTrackerTests(unittest.TestCase):
    def test_creates_spec_and_tickets_then_closes_with_receipt(self) -> None:
        runner = FakeGithubRunner()
        tracker = GithubTracker("owner/project", runner=runner)
        plan = _plan()

        references = tracker.publish(plan)
        tracker.close(
            plan.tickets[0],
            TicketReceipt(
                ticket_id="01",
                commit="abcdef1234567890",
                acceptance=(
                    AcceptanceResult("The behavior works.", True, "test passes"),
                ),
                checks=("python -m unittest: passed",),
                summary="Implemented the slice.",
            ),
        )

        self.assertEqual(
            references["01"].reference,
            "https://github.com/owner/project/issues/42",
        )
        self.assertEqual(
            [call[1:3] for call in runner.calls],
            [
                ["issue", "create"],
                ["issue", "create"],
                ["issue", "edit"],
                ["issue", "close"],
            ],
        )
        self.assertNotIn("--label", runner.calls[0])
        self.assertIn(
            "https://github.com/owner/project/issues/41",
            " ".join(runner.calls[1]),
        )


def _plan() -> DeliveryPlan:
    return DeliveryPlan(
        slug="focused-delivery",
        title="Focused delivery",
        problem_statement="The behavior is missing.",
        solution="Add it.",
        user_stories=("As a user, I can run it.",),
        implementation_decisions=("Use the existing module.",),
        testing_decisions=("Test public behavior.",),
        out_of_scope=(),
        further_notes=(),
        seams=("CLI output",),
        tickets=(
            DeliveryTicket(
                ticket_id="01",
                title="Add one slice",
                what_to_build="Expose one complete behavior.",
                blocked_by=(),
                acceptance_criteria=("The behavior works.",),
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
