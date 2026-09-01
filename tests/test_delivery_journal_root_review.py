from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_delivery_journal import (
    AdoptingTracker,
    CompleteDispatcher,
    RepairCrashDispatcher,
    StableTracker,
    StoppingDispatcher,
    _artifact_path,
    _initialize_repository,
    _interrupt_journal,
    _workflow,
)

from herdr_orchestrator.delivery import DeliveryError, StandardizedDelivery
from herdr_orchestrator.delivery_journal import (
    DeliveryEffectObservation,
    DeliveryEffectState,
    DeliveryJournal,
)
from herdr_orchestrator.delivery_protocol import DeliveryPlan, TicketReceipt
from herdr_orchestrator.delivery_recovery import DeliveryResult
from herdr_orchestrator.git_workspace import Worktree
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    TrackerBackend,
)
from herdr_orchestrator.tracker import GithubTracker, TrackerTicket


class DeliveryGithubRunner:
    def __init__(self) -> None:
        self.issues: dict[int, dict[str, str]] = {}
        self.next_issue = 41
        self.edit_count = 0
        self.close_count = 0

    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        body = ""
        if "--body-file" in argv:
            body = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
        command = argv[1:3]
        if command == ["issue", "list"]:
            stdout = json.dumps(list(self.issues.values()))
        elif command == ["issue", "create"]:
            number = self.next_issue
            self.next_issue += 1
            url = f"https://github.com/owner/project/issues/{number}"
            self.issues[number] = {
                "url": url,
                "body": body,
                "state": "OPEN",
                "title": argv[argv.index("--title") + 1],
            }
            stdout = f"{url}\n"
        elif command == ["issue", "view"]:
            stdout = json.dumps(self.issues[int(argv[3])])
        elif command == ["issue", "edit"]:
            self.issues[int(argv[3])]["body"] = body
            self.edit_count += 1
            stdout = ""
        elif command == ["issue", "close"]:
            self.issues[int(argv[3])]["state"] = "CLOSED"
            self.close_count += 1
            stdout = ""
        else:
            raise AssertionError(f"unexpected GitHub command: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class AdvisoryReviewDispatcher(CompleteDispatcher):
    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        if "受限 harness router" in prompt:
            return super().dispatch(
                workspace,
                harness,
                prompt,
                timeout_seconds=timeout_seconds,
                agent_name=agent_name,
            )
        if "Standards axis only" in prompt:
            self.prompts.append(prompt)
            _artifact_path(prompt).write_text(
                json.dumps(
                    {
                        "standards": [
                            {
                                "severity": "advisory",
                                "summary": "Consider a shorter operator note.",
                                "evidence": "README.md:1",
                                "source": "The note is longer than necessary.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")
        if "Adjudicate independent review findings" in prompt:
            self.prompts.append(prompt)
            _artifact_path(prompt).write_text(
                json.dumps(
                    {
                        "accepted": ["standards:1"],
                        "dismissed": [],
                        "rationale": "The advisory is valid but does not block delivery.",
                    }
                ),
                encoding="utf-8",
            )
            return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")
        return super().dispatch(
            workspace,
            harness,
            prompt,
            timeout_seconds=timeout_seconds,
            agent_name=agent_name,
        )


class CompletedLegacyTracker(AdoptingTracker):
    def __init__(self, external: dict[str, object], run_root: Path) -> None:
        super().__init__()
        self.external = external
        self.run_root = run_root
        self.confirmations_at_adopt: set[str] = set()

    def adopt(
        self,
        plan: object,
        *,
        references: dict[str, TrackerTicket],
        spec_url: str | None,
        markers: object,
        receipts: dict[str, TicketReceipt] | None = None,
        require_closed: bool = False,
    ) -> dict[str, TrackerTicket]:
        journal = self.run_root / "journal.jsonl"
        if journal.is_file():
            self.confirmations_at_adopt = {
                event["operation_key"]
                for event in (
                    json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
                )
                if event["event"] == "effect_confirmed" and isinstance(event["operation_key"], str)
            }
        return super().adopt(
            plan,
            references=references,
            spec_url=spec_url,
            markers=markers,
            receipts=receipts,
            require_closed=require_closed,
        )


class DeliveryJournalRootReviewTests(unittest.TestCase):
    def test_marked_ticket_human_edit_conflicts_before_dispatch_or_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            config = replace(
                config,
                standardized_delivery=replace(
                    config.standardized_delivery,
                    tracker_backend=TrackerBackend.GITHUB,
                    github_repository="owner/project",
                ),
            )
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            runner = DeliveryGithubRunner()
            interrupted = [False]

            with (
                patch.object(
                    DeliveryJournal,
                    "_persist_event",
                    _interrupt_journal(
                        DeliveryJournal._persist_event,
                        "effect_confirmed",
                        "tracker:publish",
                        interrupted,
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "journal interruption"),
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=GithubTracker("owner/project", runner=runner),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            self.assertTrue(interrupted[0])
            ticket_issue = next(
                issue for issue in runner.issues.values() if issue["title"] == "Deliver the slice"
            )
            ticket_issue["body"] = (
                ticket_issue["body"]
                .replace("**Status:** ready-for-agent", "**Status:** completed")
                .replace(
                    "- [ ] The slice is committed once.",
                    "- [x] The slice is committed once.",
                )
                + "\nHuman note outside the run-owned body.\n"
            )
            mutations = (runner.edit_count, runner.close_count)
            dispatcher = StoppingDispatcher()

            with self.assertRaisesRegex(
                DeliveryError,
                "delivery_recovery_conflict:tracker.publish",
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=dispatcher,
                    tracker=GithubTracker("owner/project", runner=runner),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)

            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            self.assertEqual(dispatcher.calls, 0)
            self.assertFalse((run_root / "worktrees/integration").exists())
            self.assertEqual((runner.edit_count, runner.close_count), mutations)

    def test_result_revalidates_repair_receipt_on_first_publication_and_replay(
        self,
    ) -> None:
        for phase in ("first", "replay"):
            with self.subTest(phase=phase):
                self._assert_repair_receipt_conflict(phase)

    def _assert_repair_receipt_conflict(self, phase: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            config = replace(
                config,
                standardized_delivery=replace(
                    config.standardized_delivery,
                    review_repair_rounds=1,
                ),
            )
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {"disable_repair_crash": True}
            delivery = StandardizedDelivery(
                config,
                dispatcher=RepairCrashDispatcher(external),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            if phase == "first":
                review = delivery._review_and_repair

                def delete_after_review(
                    plan: DeliveryPlan,
                    integration: Worktree,
                ) -> int:
                    rounds = review(plan, integration)
                    (delivery._run_root / "repairs/round-1.json").unlink()
                    return rounds

                with (
                    patch.object(
                        delivery,
                        "_review_and_repair",
                        side_effect=delete_after_review,
                    ),
                    self.assertRaisesRegex(
                        DeliveryError,
                        "delivery_recovery_conflict:repair.commit",
                    ),
                ):
                    delivery.run(goal)
                return
            result = delivery.run(goal)
            (result.artifact_root / "repairs/round-1.json").unlink()
            replay_dispatcher = RepairCrashDispatcher(external)
            with self.assertRaisesRegex(
                DeliveryError,
                "delivery_recovery_conflict:repair.commit",
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=replay_dispatcher,
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            self.assertEqual(replay_dispatcher.prompts, [])

    def test_result_revalidates_exact_adjudication_on_first_publication_and_replay(
        self,
    ) -> None:
        for phase in ("first", "replay"):
            with self.subTest(phase=phase):
                self._assert_adjudication_conflict(phase)

    def _assert_adjudication_conflict(self, phase: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            delivery = StandardizedDelivery(
                config,
                dispatcher=AdvisoryReviewDispatcher(),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )

            def change_verdict(run_root: Path) -> None:
                verdict = run_root / "reviews/round-1/verdict.json"
                verdict.write_text(
                    json.dumps(
                        {
                            "accepted": [],
                            "dismissed": ["standards:1"],
                            "rationale": "A human changed the adjudication.",
                        }
                    ),
                    encoding="utf-8",
                )

            if phase == "first":
                review = delivery._review_and_repair

                def mutate_after_review(
                    plan: DeliveryPlan,
                    integration: Worktree,
                ) -> int:
                    rounds = review(plan, integration)
                    change_verdict(delivery._run_root)
                    return rounds

                with (
                    patch.object(
                        delivery,
                        "_review_and_repair",
                        side_effect=mutate_after_review,
                    ),
                    self.assertRaisesRegex(
                        DeliveryError,
                        "delivery_recovery_conflict:review.adjudicate",
                    ),
                ):
                    delivery.run(goal)
                return
            result = delivery.run(goal)
            change_verdict(result.artifact_root)
            replay_dispatcher = AdvisoryReviewDispatcher()
            with self.assertRaisesRegex(
                DeliveryError,
                "delivery_recovery_conflict:review.adjudicate",
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=replay_dispatcher,
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            self.assertEqual(replay_dispatcher.prompts, [])

    def test_result_observer_rechecks_prerequisites_at_confirmation(self) -> None:
        for phase in ("first", "replay"):
            with self.subTest(phase=phase):
                self._assert_result_confirmation_conflict(phase)

    def _assert_result_confirmation_conflict(self, phase: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            delivery = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            if phase == "replay":
                delivery.run(goal)
                delivery = StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                )
            observe = delivery._observe_result
            mutated = [False]

            def mutate_before_confirmation(
                result: DeliveryResult,
                result_payload: dict[str, object],
                path: Path,
                expected: dict[str, object] | None,
                started: bool,
            ) -> DeliveryEffectObservation:
                if path.is_file() and not mutated[0]:
                    mutated[0] = True
                    (path.parent / "receipts/ticket-01.json").unlink()
                return observe(result, result_payload, path, expected, started)

            with (
                patch.object(
                    delivery,
                    "_observe_result",
                    side_effect=mutate_before_confirmation,
                ),
                self.assertRaisesRegex(
                    DeliveryError,
                    "delivery_recovery_conflict:result.publish",
                ),
            ):
                delivery.run(goal)
            self.assertTrue(mutated[0])

    def test_result_confirmation_rechecks_after_matched_observation(self) -> None:
        for phase in ("first", "replay"):
            for mutation in ("receipt", "tracker-close"):
                with self.subTest(phase=phase, mutation=mutation):
                    self._assert_post_matched_result_conflict(phase, mutation)

    def _assert_post_matched_result_conflict(
        self,
        phase: str,
        mutation: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            delivery = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            if phase == "replay":
                delivery.run(goal)
                delivery = StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                )
            observe = delivery._observe_result
            mutated = [False]

            def mutate_after_match(
                result: DeliveryResult,
                result_payload: dict[str, object],
                path: Path,
                expected: dict[str, object] | None,
                started: bool,
            ) -> DeliveryEffectObservation:
                observation = observe(
                    result,
                    result_payload,
                    path,
                    expected,
                    started,
                )
                if observation.state is DeliveryEffectState.MATCHED and not mutated[0]:
                    mutated[0] = True
                    if mutation == "receipt":
                        (path.parent / "receipts/ticket-01.json").unlink()
                    else:
                        external["closed"] = False
                return observation

            with (
                patch.object(
                    delivery,
                    "_observe_result",
                    side_effect=mutate_after_match,
                ),
                self.assertRaisesRegex(
                    DeliveryError,
                    "delivery_recovery_conflict:result.publish",
                ),
            ):
                delivery.run(goal)
            self.assertTrue(mutated[0])

    def test_completed_pre_journal_run_rebuilds_before_marker_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            config = replace(
                config,
                standardized_delivery=replace(
                    config.standardized_delivery,
                    tracker_backend=TrackerBackend.GITHUB,
                    github_repository="owner/project",
                ),
            )
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            result = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            run_root = result.artifact_root
            before_log = subprocess.run(
                ["git", "log", "--format=%s"],
                cwd=run_root / "worktrees/integration",
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            mutation_counts = (
                external["publish_mutations"],
                external["close_mutations"],
            )
            (run_root / "journal.jsonl").unlink()
            (run_root / "run-owner.json").unlink()
            tracker = CompletedLegacyTracker(external, run_root)
            dispatcher = CompleteDispatcher()

            recovered = StandardizedDelivery(
                config,
                dispatcher=dispatcher,
                tracker=tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)

            required_before_adopt = {
                "agent:artifact:plan",
                "git:worktree:integration",
                "git:worktree:ticket:01",
                "ticket:accept:01",
                "git:merge:01",
                "tracker:close:01",
                "agent:artifact:review-standards-1",
                "agent:artifact:review-spec-1",
                "review:accept:1",
                "result:publish",
            }
            after_log = subprocess.run(
                ["git", "log", "--format=%s"],
                cwd=run_root / "worktrees/integration",
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(recovered, result)
            self.assertEqual(tracker.adopt_calls, 1)
            self.assertTrue(required_before_adopt.issubset(tracker.confirmations_at_adopt))
            self.assertEqual(dispatcher.prompts, [])
            self.assertEqual(after_log, before_log)
            self.assertEqual(
                (external["publish_mutations"], external["close_mutations"]),
                mutation_counts,
            )

    def test_completed_pre_journal_run_rebuilds_repair_history_before_adoption(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            config = replace(
                config,
                standardized_delivery=replace(
                    config.standardized_delivery,
                    tracker_backend=TrackerBackend.GITHUB,
                    github_repository="owner/project",
                    review_repair_rounds=1,
                ),
            )
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {"disable_repair_crash": True}
            result = StandardizedDelivery(
                config,
                dispatcher=RepairCrashDispatcher(external),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            run_root = result.artifact_root
            mutation_counts = (
                external["publish_mutations"],
                external["close_mutations"],
                external["repair_commits"],
            )
            (run_root / "journal.jsonl").unlink()
            (run_root / "run-owner.json").unlink()
            tracker = CompletedLegacyTracker(external, run_root)
            dispatcher = RepairCrashDispatcher(external)

            recovered = StandardizedDelivery(
                config,
                dispatcher=dispatcher,
                tracker=tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)

            required_before_adopt = {
                "review:accept:1",
                "agent:artifact:judge-1",
                "repair:commit:1",
                "agent:artifact:review-standards-2",
                "agent:artifact:review-spec-2",
                "review:accept:2",
                "result:publish",
            }
            self.assertEqual(recovered, result)
            self.assertEqual(tracker.adopt_calls, 1)
            self.assertTrue(required_before_adopt.issubset(tracker.confirmations_at_adopt))
            self.assertEqual(dispatcher.prompts, [])
            self.assertEqual(
                (
                    external["publish_mutations"],
                    external["close_mutations"],
                    external["repair_commits"],
                ),
                mutation_counts,
            )

    def test_completed_pre_journal_reconstruction_resumes_after_result_intent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            config = replace(
                config,
                standardized_delivery=replace(
                    config.standardized_delivery,
                    tracker_backend=TrackerBackend.GITHUB,
                    github_repository="owner/project",
                ),
            )
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            result = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            run_root = result.artifact_root
            (run_root / "journal.jsonl").unlink()
            (run_root / "run-owner.json").unlink()
            interrupted = [False]

            with (
                patch.object(
                    DeliveryJournal,
                    "_persist_event",
                    _interrupt_journal(
                        DeliveryJournal._persist_event,
                        "effect_intent",
                        "result:publish",
                        interrupted,
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "journal interruption"),
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=CompletedLegacyTracker(external, run_root),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            self.assertTrue(interrupted[0])
            first_intent = next(
                event
                for event in (
                    json.loads(line)
                    for line in (run_root / "journal.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                if event["event"] == "effect_intent" and event["operation_key"] == "tracker:publish"
            )
            tracker = CompletedLegacyTracker(external, run_root)

            recovered = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            final_intent = next(
                event
                for event in (
                    json.loads(line)
                    for line in (run_root / "journal.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                if event["event"] == "effect_intent" and event["operation_key"] == "tracker:publish"
            )

            self.assertEqual(recovered, result)
            self.assertEqual(final_intent["details"], first_intent["details"])
            self.assertEqual(tracker.adopt_calls, 1)

    def test_completed_pre_journal_conflict_has_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            config = replace(
                config,
                standardized_delivery=replace(
                    config.standardized_delivery,
                    tracker_backend=TrackerBackend.GITHUB,
                    github_repository="owner/project",
                ),
            )
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            result = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            run_root = result.artifact_root
            (run_root / "journal.jsonl").unlink()
            (run_root / "run-owner.json").unlink()
            (run_root / "reviews/round-1/standards.json").unlink()
            mutation_counts = (
                external["publish_mutations"],
                external["close_mutations"],
            )
            before_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=run_root / "worktrees/integration",
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            tracker = CompletedLegacyTracker(external, run_root)
            dispatcher = CompleteDispatcher()

            with self.assertRaises(DeliveryError):
                StandardizedDelivery(
                    config,
                    dispatcher=dispatcher,
                    tracker=tracker,
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)

            after_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=run_root / "worktrees/integration",
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(tracker.adopt_calls, 0)
            self.assertEqual(dispatcher.prompts, [])
            self.assertEqual(after_head, before_head)
            self.assertEqual(
                (external["publish_mutations"], external["close_mutations"]),
                mutation_counts,
            )


if __name__ == "__main__":
    unittest.main()
