from __future__ import annotations

import json
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
    tracker_markers,
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


class StatefulGithubRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.issues: dict[int, dict[str, str]] = {}
        self.next_issue = 41
        self.create_count = 0
        self.edit_count = 0
        self.close_count = 0
        self.crash_after_create = False
        self.crash_after_close = False

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
        body = ""
        if "--body-file" in argv:
            body = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
        command = argv[1:3]
        if command == ["issue", "list"]:
            limit = int(argv[argv.index("--limit") + 1])
            stdout = json.dumps(list(self.issues.values())[:limit])
        elif command == ["issue", "create"]:
            number = self.next_issue
            self.next_issue += 1
            self.create_count += 1
            url = f"https://github.com/owner/project/issues/{number}"
            title = argv[argv.index("--title") + 1]
            self.issues[number] = {
                "number": str(number),
                "url": url,
                "body": body,
                "state": "OPEN",
                "title": title,
            }
            if self.crash_after_create:
                self.crash_after_create = False
                raise OSError("process died after issue creation")
            stdout = f"{url}\n"
        elif command == ["issue", "view"]:
            issue = self.issues[int(argv[3])]
            stdout = json.dumps(issue)
        elif command == ["issue", "edit"]:
            issue = self.issues[int(argv[3])]
            issue["body"] = body
            self.edit_count += 1
            stdout = ""
        elif command == ["issue", "close"]:
            issue = self.issues[int(argv[3])]
            issue["state"] = "CLOSED"
            self.close_count += 1
            if self.crash_after_close:
                self.crash_after_close = False
                raise OSError("process died after issue close")
            stdout = ""
        else:
            raise AssertionError(f"unexpected GitHub command: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class GithubTrackerTests(unittest.TestCase):
    def test_delivery_markers_include_a_run_owned_publication_nonce(self) -> None:
        plan = _plan()
        first = tracker_markers("c" * 12, plan)
        second = tracker_markers("c" * 12, plan)

        self.assertNotEqual(first.spec, second.spec)
        self.assertRegex(
            first.spec,
            r"^<!-- herdr-delivery:run=c{12}:nonce=[0-9a-f]{32}:kind=spec -->$",
        )
        self.assertIn(f":nonce={first.nonce}:", first.ticket("01"))

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

    def test_reuses_exact_markers_after_publication_process_death(self) -> None:
        runner = StatefulGithubRunner()
        plan = _plan()
        markers = tracker_markers("a" * 12, plan)
        runner.crash_after_create = True

        with self.assertRaisesRegex(TrackerError, "github_unavailable"):
            GithubTracker("owner/project", runner=runner).publish(plan, markers=markers)

        tracker = GithubTracker("owner/project", runner=runner)
        references = tracker.publish(plan, markers=markers)

        self.assertEqual(runner.create_count, 2)
        self.assertEqual(
            references["01"].reference,
            "https://github.com/owner/project/issues/42",
        )
        self.assertEqual(tracker.spec_url, "https://github.com/owner/project/issues/41")
        self.assertEqual(
            sum(markers.spec in issue["body"] for issue in runner.issues.values()),
            1,
        )
        self.assertEqual(
            sum(markers.ticket("01") in issue["body"] for issue in runner.issues.values()),
            1,
        )
        searches = [
            call[call.index("--search") + 1]
            for call in runner.calls
            if call[1:3] == ["issue", "list"]
        ]
        self.assertTrue(all(search == f'"{markers.nonce}" in:body' for search in searches))

    def test_rejects_a_closed_marker_match_during_publication_replay(self) -> None:
        runner = StatefulGithubRunner()
        plan = _plan()
        markers = tracker_markers("d" * 12, plan)
        runner.crash_after_create = True
        with self.assertRaisesRegex(TrackerError, "github_unavailable"):
            GithubTracker("owner/project", runner=runner).publish(plan, markers=markers)
        runner.issues[41]["state"] = "CLOSED"

        with self.assertRaisesRegex(TrackerError, "github_marker_conflict"):
            GithubTracker("owner/project", runner=runner).publish(plan, markers=markers)

        self.assertEqual(runner.create_count, 1)

    def test_replays_maximum_plan_without_hiding_the_101st_marker(self) -> None:
        tickets = tuple(
            DeliveryTicket(
                ticket_id=f"{number:02d}" if number < 100 else "100",
                title=f"Add slice {number}",
                what_to_build=f"Expose slice {number}.",
                blocked_by=(),
                acceptance_criteria=(f"Slice {number} works.",),
            )
            for number in range(1, 101)
        )
        plan = replace(_plan(), tickets=tickets)
        markers = tracker_markers("2" * 12, plan)
        runner = StatefulGithubRunner()
        first = GithubTracker("owner/project", runner=runner)
        references = first.publish(plan, markers=markers)

        replay = GithubTracker("owner/project", runner=runner)
        replayed = replay.publish(plan, markers=markers)

        self.assertEqual(len(references), 100)
        self.assertEqual(replayed, references)
        self.assertEqual(runner.create_count, 101)
        limits = {
            call[call.index("--limit") + 1]
            for call in runner.calls
            if call[1:3] == ["issue", "list"]
        }
        self.assertEqual(limits, {"1000"})

    def test_reconciles_close_after_remote_close_before_confirmation(self) -> None:
        runner = StatefulGithubRunner()
        plan = _plan()
        markers = tracker_markers("b" * 12, plan)
        tracker = GithubTracker("owner/project", runner=runner)
        references = tracker.publish(plan, markers=markers)
        receipt = TicketReceipt(
            ticket_id="01",
            commit="abcdef1234567890",
            acceptance=(AcceptanceResult("The behavior works.", True, "test passes"),),
            checks=("python -m unittest: passed",),
            summary="Implemented the slice.",
        )
        runner.crash_after_close = True

        with self.assertRaisesRegex(TrackerError, "github_unavailable"):
            tracker.close(plan.tickets[0], receipt, marker=markers.ticket("01"))

        resumed = GithubTracker("owner/project", runner=runner)
        resumed.references = references
        resumed.spec_url = tracker.spec_url
        resumed.close(plan.tickets[0], receipt, marker=markers.ticket("01"))

        self.assertEqual(runner.edit_count, 1)
        self.assertEqual(runner.close_count, 1)
        ticket_number = int(references["01"].reference.rsplit("/", 1)[1])
        self.assertEqual(runner.issues[ticket_number]["state"], "CLOSED")
        self.assertIn("**Status:** completed", runner.issues[ticket_number]["body"])

    def test_adopts_exact_pre_journal_issues_without_changing_identity(self) -> None:
        runner = StatefulGithubRunner()
        plan = _plan()
        legacy = GithubTracker("owner/project", runner=runner)
        references = legacy.publish(plan)
        spec_url = legacy.spec_url
        self.assertIsNotNone(spec_url)
        markers = tracker_markers("e" * 12, plan)

        adopted = GithubTracker("owner/project", runner=runner)
        result = adopted.adopt(
            plan,
            references=references,
            spec_url=spec_url,
            markers=markers,
        )

        self.assertEqual(result, references)
        self.assertEqual(runner.create_count, 2)
        self.assertEqual(runner.edit_count, 2)
        self.assertEqual(adopted.spec_url, spec_url)
        self.assertIn(markers.spec, runner.issues[41]["body"])
        self.assertIn(markers.ticket("01"), runner.issues[42]["body"])

    def test_adopts_an_exact_completed_pre_journal_ticket(self) -> None:
        runner = StatefulGithubRunner()
        plan = _plan()
        legacy = GithubTracker("owner/project", runner=runner)
        references = legacy.publish(plan)
        receipt = TicketReceipt(
            ticket_id="01",
            commit="abcdef1234567890",
            acceptance=(AcceptanceResult("The behavior works.", True, "test passes"),),
            checks=("python -m unittest: passed",),
            summary="Implemented the slice.",
        )
        legacy.close(plan.tickets[0], receipt)
        markers = tracker_markers("f" * 12, plan)
        adopted = GithubTracker("owner/project", runner=runner)

        adopted.adopt(
            plan,
            references=references,
            spec_url=legacy.spec_url,
            markers=markers,
            receipts={"01": receipt},
        )
        adopted.close(plan.tickets[0], receipt, marker=markers.ticket("01"))

        self.assertEqual(runner.create_count, 2)
        self.assertEqual(runner.edit_count, 3)
        self.assertEqual(runner.close_count, 1)
        self.assertEqual(runner.issues[42]["state"], "CLOSED")
        self.assertIn(markers.ticket("01"), runner.issues[42]["body"])

    def test_legacy_adoption_rejects_a_human_modified_completed_body_before_edits(
        self,
    ) -> None:
        runner = StatefulGithubRunner()
        plan = _plan()
        legacy = GithubTracker("owner/project", runner=runner)
        references = legacy.publish(plan)
        receipt = TicketReceipt(
            ticket_id="01",
            commit="abcdef1234567890",
            acceptance=(AcceptanceResult("The behavior works.", True, "test passes"),),
            checks=("python -m unittest: passed",),
            summary="Implemented the slice.",
        )
        legacy.close(plan.tickets[0], receipt)
        runner.issues[42]["body"] += "\nHuman note that is not run-owned.\n"
        edit_count = runner.edit_count

        with self.assertRaisesRegex(TrackerError, "github_adoption_conflict"):
            GithubTracker("owner/project", runner=runner).adopt(
                plan,
                references=references,
                spec_url=legacy.spec_url,
                markers=tracker_markers("1" * 12, plan),
                receipts={"01": receipt},
            )

        self.assertEqual(runner.edit_count, edit_count)

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
