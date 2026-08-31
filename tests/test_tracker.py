from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from herdr_orchestrator.delivery_protocol import (
    AcceptanceResult,
    DeliveryPlan,
    DeliveryTicket,
    TicketReceipt,
)
from herdr_orchestrator.tracker import (
    GithubTracker,
    LocalMarkdownTracker,
    TrackerError,
    TrackerTicket,
    render_spec,
)


class LocalMarkdownTrackerTests(unittest.TestCase):
    def test_rejects_symlinked_tracker_root_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            tracker_root = root / "tracker"
            tracker_root.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(TrackerError, "local_tracker_path_symlink"):
                LocalMarkdownTracker(tracker_root).publish(_plan())
            self.assertEqual(tuple(outside.iterdir()), ())

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
                    acceptance=(AcceptanceResult("The behavior works.", True, "test passes"),),
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

    def test_rejects_tracker_path_escape_from_unvalidated_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tracker"
            with self.assertRaisesRegex(TrackerError, "local_tracker_path_invalid"):
                LocalMarkdownTracker(root).publish(_plan(slug="../outside"))
            self.assertFalse((Path(temporary) / "outside").exists())

    def test_rejects_symlinked_tracker_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tracker"
            plan = _plan()
            feature_root = root / plan.slug
            issues_root = feature_root / "issues"
            issues_root.mkdir(parents=True)
            (feature_root / "spec.md").write_text(
                render_spec(plan),
                encoding="utf-8",
            )
            target = Path(temporary) / "outside.md"
            target.write_text("untouched", encoding="utf-8")
            (issues_root / "01-add-one-slice.md").symlink_to(target)

            with self.assertRaisesRegex(TrackerError, "local_tracker_path_symlink"):
                LocalMarkdownTracker(root).publish(plan)
            self.assertEqual(target.read_text(encoding="utf-8"), "untouched")

    def test_maps_invalid_existing_tracker_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tracker"
            plan = _plan()
            LocalMarkdownTracker(root).publish(plan)
            (root / plan.slug / "spec.md").write_bytes(b"\xff")

            with self.assertRaisesRegex(TrackerError, "local_artifact_invalid_encoding"):
                LocalMarkdownTracker(root).publish(plan)


class FakeGithubRunner:
    def __init__(self, *, issue_url: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.body_contents: list[str] = []
        self.issue_number = 40
        self.issue_url = issue_url

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
        if "--body-file" in argv:
            body_path = Path(argv[argv.index("--body-file") + 1])
            self.body_contents.append(body_path.read_text(encoding="utf-8"))
        elif "--body" in argv:
            self.body_contents.append(argv[argv.index("--body") + 1])
        stdout = ""
        if argv[1:3] == ["issue", "create"]:
            self.issue_number += 1
            stdout = (
                f"{self.issue_url}\n"
                if self.issue_url is not None
                else f"https://github.com/owner/project/issues/{self.issue_number}\n"
            )
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class GithubTrackerTests(unittest.TestCase):
    def test_rejects_secret_material_before_creating_issue(self) -> None:
        runner = FakeGithubRunner()
        secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"  # pragma: allowlist secret
        plan = replace(
            _plan(),
            problem_statement=f"Configure token={secret} for the service.",
        )

        with self.assertRaisesRegex(TrackerError, "github_secret_material"):
            GithubTracker("owner/project", runner=runner).publish(plan)

        self.assertEqual(runner.calls, [])

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
                acceptance=(AcceptanceResult("The behavior works.", True, "test passes"),),
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
            runner.body_contents[1],
        )

    def test_transports_multiline_bodies_through_body_files(self) -> None:
        runner = FakeGithubRunner()
        tracker = GithubTracker("owner/project", runner=runner)
        plan = _plan()

        tracker.publish(plan)
        tracker.close(
            plan.tickets[0],
            TicketReceipt(
                ticket_id="01",
                commit="abcdef1234567890",
                acceptance=(AcceptanceResult("The behavior works.", True, "test passes"),),
                checks=("python -m unittest: passed",),
                summary="Implemented the slice.",
            ),
        )

        body_calls = [call for call in runner.calls if "--body-file" in call]
        self.assertEqual(len(body_calls), 3)
        self.assertTrue(all("--body" not in call for call in runner.calls))
        self.assertIn("## Problem Statement\n\n", runner.body_contents[0])
        self.assertIn(
            "## Parent\n\nhttps://github.com/owner/project/issues/41\n\n",
            runner.body_contents[1],
        )
        self.assertIn("**Status:** completed", runner.body_contents[2])
        self.assertTrue(all("\\n" not in body for body in runner.body_contents))

    def test_rejects_issue_urls_outside_configured_repository_or_shape(self) -> None:
        invalid_urls = (
            "https://github.com/other/project/issues/41",
            "https://github.com/owner/project/issues/not-a-number",
            "https://github.com/owner/project/pull/41",
            "http://github.com/owner/project/issues/41",
            "https://github.com/owner/project/issues/41?query=1",
        )
        for issue_url in invalid_urls:
            with self.subTest(issue_url=issue_url):
                runner = FakeGithubRunner(issue_url=issue_url)
                with self.assertRaisesRegex(
                    TrackerError,
                    "github_issue_create_invalid_response",
                ):
                    GithubTracker("owner/project", runner=runner).publish(_plan())

    def test_maps_invalid_runner_output_to_stable_tracker_errors(self) -> None:
        for stdout in (None, b"https://github.com/owner/project/issues/41\n"):
            with self.subTest(stdout=stdout):

                def runner(
                    argv: list[str],
                    *,
                    output: object = stdout,
                    **_: object,
                ) -> subprocess.CompletedProcess[str]:
                    return subprocess.CompletedProcess(argv, 0, output, "")  # type: ignore[arg-type]

                with self.assertRaisesRegex(
                    TrackerError,
                    "github_issue_create_invalid_response",
                ):
                    GithubTracker("owner/project", runner=runner).publish(_plan())

        def decode_failure(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        with self.assertRaisesRegex(TrackerError, "github_invalid_response"):
            GithubTracker("owner/project", runner=decode_failure).publish(_plan())

        class MissingStdoutRunner:
            def __call__(self, argv: list[str], **_: object) -> object:
                return type("Process", (), {"returncode": 0})()

        with self.assertRaisesRegex(TrackerError, "github_invalid_response"):
            GithubTracker("owner/project", runner=MissingStdoutRunner()).publish(_plan())

    def test_rejects_tampered_ticket_reference_before_editing(self) -> None:
        runner = FakeGithubRunner()
        tracker = GithubTracker("owner/project", runner=runner)
        plan = _plan()
        tracker.publish(plan)
        tracker.references["01"] = TrackerTicket(
            "01",
            "https://github.com/other/project/issues/999",
        )

        with self.assertRaisesRegex(TrackerError, "github_ticket_reference_invalid"):
            tracker.close(
                plan.tickets[0],
                TicketReceipt(
                    ticket_id="01",
                    commit="abcdef1234567890",
                    acceptance=(AcceptanceResult("The behavior works.", True, "test passes"),),
                    checks=("python -m unittest: passed",),
                    summary="Implemented the slice.",
                ),
            )


def _plan(*, slug: str = "focused-delivery") -> DeliveryPlan:
    return DeliveryPlan(
        slug=slug,
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
