from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest.mock import patch

from crash_matrix import CrashInjected, run_public_operation_crash_matrix
from test_delivery_journal import (
    AdoptingTracker,
    CompleteDispatcher,
    CrashAfterCloseTracker,
    CrashAfterFirstOfTwoTracker,
    CrashLocalMarkdownTracker,
    PlanningThenStoppingDispatcher,
    ProxyResponseCrashDispatcher,
    RepairCrashDispatcher,
    StableTracker,
    TwoTicketDispatcher,
    _git,
    _initialize_repository,
    _interrupt_before_confirmation,
    _interrupt_journal,
    _mutate_final_prerequisite,
    _workflow,
)

import herdr_orchestrator.delivery_recovery as recovery_module
from herdr_orchestrator.delivery import DeliveryError, StandardizedDelivery
from herdr_orchestrator.delivery_journal import DeliveryJournal
from herdr_orchestrator.delivery_protocol import DeliveryPlan
from herdr_orchestrator.delivery_recovery import DeliveryResult
from herdr_orchestrator.git_workspace import Worktree
from herdr_orchestrator.model import Harness, TrackerBackend, WorkflowConfig


class DeliveryJournalRecoveryTests(unittest.TestCase):
    def test_receipt_merge_and_close_recover_in_durable_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            first_tracker = CrashAfterCloseTracker(external)
            first = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=first_tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )

            with self.assertRaisesRegex(RuntimeError, "tracker died after close"):
                first.run(goal)

            second_tracker = CrashAfterCloseTracker(external)
            second = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=second_tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            result = second.run(goal)
            events = [
                json.loads(line)
                for line in (result.artifact_root / "journal.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            confirmations = {
                event["operation_key"]: event["sequence"]
                for event in events
                if event["event"] == "effect_confirmed"
            }
            close_intent = next(
                event
                for event in events
                if event["operation_key"] == "tracker:close:01"
                and event["event"] == "effect_intent"
            )
            log = _git(
                result.artifact_root / "worktrees/integration",
                "log",
                "--format=%s",
            ).stdout

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(external["close_mutations"], 1)
            self.assertEqual(first_tracker.close_calls, 1)
            self.assertEqual(second_tracker.close_calls, 0)
            self.assertLess(
                confirmations["ticket:accept:01"],
                confirmations["git:merge:01"],
            )
            self.assertLess(confirmations["git:merge:01"], close_intent["sequence"])
            self.assertLess(
                close_intent["sequence"],
                confirmations["tracker:close:01"],
            )
            self.assertEqual(log.count("Merge branch"), 1)

    def test_final_result_recovers_after_write_before_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            first_dispatcher = CompleteDispatcher()
            tracker_external: dict[str, object] = {}
            first_tracker = StableTracker(tracker_external)
            first = StandardizedDelivery(
                config,
                dispatcher=first_dispatcher,
                tracker=first_tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            original_write = recovery_module._write_json

            def interrupt_after_result(path: Path, payload: dict[str, object]) -> None:
                original_write(path, payload)
                if path.name == "result.json":
                    raise RuntimeError("process died after result write")

            with (
                patch.object(recovery_module, "_write_json", side_effect=interrupt_after_result),
                self.assertRaisesRegex(RuntimeError, "process died after result write"),
            ):
                first.run(goal)

            second_dispatcher = CompleteDispatcher()
            second_tracker = StableTracker(tracker_external)
            result = StandardizedDelivery(
                config,
                dispatcher=second_dispatcher,
                tracker=second_tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            events = [
                json.loads(line)
                for line in (result.artifact_root / "journal.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            result_events = [
                event for event in events if event["operation_key"] == "result:publish"
            ]
            review_confirmation = next(
                event
                for event in events
                if event["operation_key"] == "review:accept:1"
                and event["event"] == "effect_confirmed"
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(second_dispatcher.prompts, [])
            self.assertEqual(second_tracker.publish_calls, 0)
            self.assertEqual(second_tracker.close_calls, 0)
            self.assertEqual(
                [event["event"] for event in result_events],
                ["effect_intent", "effect_started", "effect_confirmed"],
            )
            self.assertLess(
                review_confirmation["sequence"],
                result_events[0]["sequence"],
            )

    def test_final_result_rejects_a_commit_after_final_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            delivery = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=StableTracker(),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            review = delivery._review_and_repair

            def commit_after_review(plan: DeliveryPlan, integration: Worktree) -> int:
                rounds = review(plan, integration)
                integration_path = integration.path
                marker = integration_path / "unreviewed.txt"
                marker.write_text("not reviewed\n", encoding="utf-8")
                _git(integration_path, "add", marker.name)
                _git(integration_path, "commit", "-m", "feat: unreviewed change")
                return rounds

            with (
                patch.object(
                    delivery,
                    "_review_and_repair",
                    side_effect=commit_after_review,
                ),
                self.assertRaisesRegex(
                    DeliveryError,
                    "delivery_recovery_conflict:review.accept",
                ),
            ):
                delivery.run(goal)

    def test_first_result_publication_freshly_observes_all_prerequisites(self) -> None:
        mutations = (
            ("receipt", "delivery_recovery_conflict:receipt.ticket.accept"),
            ("tracker-close", "delivery_recovery_conflict:tracker.close"),
            ("review", "delivery_recovery_conflict:review.accept"),
        )
        for mutation, expected_error in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary).resolve() / "repository"
                _initialize_repository(repository)
                config = _workflow(repository)
                goal = repository / "goal.md"
                goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
                external: dict[str, object] = {}
                dispatcher = CompleteDispatcher()
                delivery = StandardizedDelivery(
                    config,
                    dispatcher=dispatcher,
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                )
                review = delivery._review_and_repair

                def mutate_after_review(
                    plan: DeliveryPlan,
                    integration: Worktree,
                    current_mutation: str = mutation,
                    current_review: Callable[[DeliveryPlan, Worktree], int] = review,
                    current_delivery: StandardizedDelivery = delivery,
                    current_external: dict[str, object] = external,
                ) -> int:
                    rounds = current_review(plan, integration)
                    _mutate_final_prerequisite(
                        current_delivery._run_root,
                        current_external,
                        current_mutation,
                    )
                    return rounds

                with (
                    patch.object(
                        delivery,
                        "_review_and_repair",
                        side_effect=mutate_after_review,
                    ),
                    self.assertRaisesRegex(DeliveryError, expected_error),
                ):
                    delivery.run(goal)

    def test_completed_result_freshly_reobserves_all_prerequisites(self) -> None:
        mutations = (
            ("receipt", "delivery_recovery_conflict:receipt.ticket.accept"),
            ("tracker-close", "delivery_recovery_conflict:tracker.close"),
            ("review", "delivery_recovery_conflict:review.accept"),
        )
        for mutation, expected_error in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary).resolve() / "repository"
                _initialize_repository(repository)
                config = _workflow(repository)
                goal = repository / "goal.md"
                goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
                external: dict[str, object] = {}
                first_dispatcher = CompleteDispatcher()
                result = StandardizedDelivery(
                    config,
                    dispatcher=first_dispatcher,
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
                _mutate_final_prerequisite(
                    result.artifact_root,
                    external,
                    mutation,
                )
                replay_dispatcher = CompleteDispatcher()

                with self.assertRaisesRegex(DeliveryError, expected_error):
                    StandardizedDelivery(
                        config,
                        dispatcher=replay_dispatcher,
                        tracker=StableTracker(external),
                        controller_harness=Harness.DROID,
                        worker_harnesses=(Harness.DROID,),
                    ).run(goal)
                self.assertEqual(replay_dispatcher.prompts, [])

    def test_repair_commit_recovers_without_a_second_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
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
            external: dict[str, object] = {}
            first = StandardizedDelivery(
                config,
                dispatcher=RepairCrashDispatcher(external),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "worker died after repair commit"):
                first.run(goal)

            result = StandardizedDelivery(
                config,
                dispatcher=RepairCrashDispatcher(external),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            events = [
                json.loads(line)
                for line in (result.artifact_root / "journal.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            repair_events = [
                event for event in events if event["operation_key"] == "repair:commit:1"
            ]
            log = _git(
                result.artifact_root / "worktrees/integration",
                "log",
                "--format=%s",
            ).stdout

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.review_rounds, 2)
            self.assertEqual(external["repair_commits"], 1)
            self.assertEqual(log.count("fix: repair accepted finding"), 1)
            self.assertEqual(
                [event["event"] for event in repair_events],
                ["effect_intent", "effect_started", "effect_confirmed"],
            )

    def test_repair_head_change_without_receipt_is_a_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
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
            external: dict[str, object] = {"omit_repair_receipt": True}
            first = StandardizedDelivery(
                config,
                dispatcher=RepairCrashDispatcher(external),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "worker died after repair commit"):
                first.run(goal)

            resumed_dispatcher = RepairCrashDispatcher(external)
            resumed = StandardizedDelivery(
                config,
                dispatcher=resumed_dispatcher,
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(
                DeliveryError,
                "delivery_recovery_conflict:repair.commit",
            ):
                resumed.run(goal)

            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            repair_state = json.loads((run_root / "repair-state.json").read_text(encoding="utf-8"))
            self.assertEqual(external["repair_commits"], 1)
            self.assertEqual(repair_state["attempts"], 0)
            self.assertIsNotNone(repair_state["in_flight"])

    def test_repair_crash_matrix_converges_before_and_after_commit(self) -> None:
        for transition in ("effect_intent", "effect_confirmed"):
            with self.subTest(transition=transition), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary).resolve() / "repository"
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
                interrupted = [False]
                interrupt = _interrupt_journal(
                    DeliveryJournal._persist_event,
                    transition,
                    "repair:commit:1",
                    interrupted,
                )

                with (
                    patch.object(DeliveryJournal, "_persist_event", interrupt),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "journal interruption",
                    ),
                ):
                    StandardizedDelivery(
                        config,
                        dispatcher=RepairCrashDispatcher(external),
                        tracker=StableTracker(external),
                        controller_harness=Harness.DROID,
                        worker_harnesses=(Harness.DROID,),
                    ).run(goal)
                self.assertTrue(interrupted[0])

                result = StandardizedDelivery(
                    config,
                    dispatcher=RepairCrashDispatcher(external),
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)

                self.assertEqual(result.status, "succeeded")
                self.assertEqual(result.review_rounds, 2)
                self.assertEqual(external["repair_commits"], 1)

    def test_pre_journal_tracker_publication_is_adopted_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
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
            first = StandardizedDelivery(
                config,
                dispatcher=PlanningThenStoppingDispatcher(),
                tracker=StableTracker(),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "stop after tracker recovery"):
                first.run(goal)
            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            (run_root / "journal.jsonl").unlink()
            (run_root / "run-owner.json").unlink()

            tracker = AdoptingTracker()
            resumed = StandardizedDelivery(
                config,
                dispatcher=PlanningThenStoppingDispatcher(),
                tracker=tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "stop after tracker recovery"):
                resumed.run(goal)

            events = [
                json.loads(line)
                for line in (run_root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            publication = [event for event in events if event["operation_key"] == "tracker:publish"]
            self.assertEqual(tracker.adopt_calls, 1)
            self.assertEqual(
                [event["event"] for event in publication],
                ["effect_intent", "effect_started", "effect_confirmed"],
            )

    def test_legacy_receipt_conflict_causes_zero_tracker_adoption_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
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
            with self.assertRaisesRegex(RuntimeError, "tracker died after close"):
                StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=CrashAfterCloseTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            (run_root / "journal.jsonl").unlink()
            (run_root / "run-owner.json").unlink()
            receipt_path = run_root / "receipts/ticket-01.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["commit"] = "f" * 40
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            tracker = AdoptingTracker()

            with self.assertRaisesRegex(
                DeliveryError,
                "ticket_receipt_commit_mismatch|delivery_recovery_conflict",
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=tracker,
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            self.assertEqual(tracker.adopt_calls, 0)

    def test_proxy_response_recovers_without_sending_the_answer_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            first = StandardizedDelivery(
                config,
                dispatcher=ProxyResponseCrashDispatcher(external),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "process died after proxy response"):
                first.run(goal)

            result = StandardizedDelivery(
                config,
                dispatcher=ProxyResponseCrashDispatcher(external),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            events = [
                json.loads(line)
                for line in (result.artifact_root / "journal.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            responses = [event for event in events if event["effect_kind"] == "agent.respond"]

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(external["response_calls"], 1)
            self.assertEqual(
                [event["event"] for event in responses],
                ["effect_intent", "effect_started", "effect_confirmed"],
            )

    def test_pre_journal_receipt_merge_and_close_are_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            first = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=CrashLocalMarkdownTracker(
                    config.standardized_delivery.tracker_root,
                    external,
                ),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "legacy process died after close"):
                first.run(goal)
            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            (run_root / "journal.jsonl").unlink()
            (run_root / "run-owner.json").unlink()

            result = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=CrashLocalMarkdownTracker(
                    config.standardized_delivery.tracker_root,
                    external,
                ),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            events = [
                json.loads(line)
                for line in (run_root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            confirmations = {
                event["operation_key"] for event in events if event["event"] == "effect_confirmed"
            }
            log = _git(
                result.artifact_root / "worktrees/integration",
                "log",
                "--format=%s",
            ).stdout

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(external["close_calls"], 1)
            self.assertEqual(log.count("Merge branch"), 1)
            self.assertTrue(
                {
                    "git:worktree:integration",
                    "git:worktree:ticket:01",
                    "ticket:accept:01",
                    "git:merge:01",
                    "tracker:close:01",
                }.issubset(confirmations)
            )

    def test_parallel_sibling_survives_crash_after_first_ticket_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver two recoverable slices.", encoding="utf-8")
            external: dict[str, object] = {}
            first = StandardizedDelivery(
                config,
                dispatcher=TwoTicketDispatcher(),
                tracker=CrashAfterFirstOfTwoTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "process died after closing ticket 01",
            ):
                first.run(goal)

            second_dispatcher = TwoTicketDispatcher()
            result = StandardizedDelivery(
                config,
                dispatcher=second_dispatcher,
                tracker=CrashAfterFirstOfTwoTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            log = _git(
                result.artifact_root / "worktrees/integration",
                "log",
                "--format=%s",
            ).stdout

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.tickets_completed, 2)
            self.assertEqual(external["close_mutations_01"], 1)
            self.assertEqual(external["close_mutations_02"], 1)
            self.assertEqual(log.count("Merge branch"), 2)
            self.assertFalse(
                any(
                    "Implement exactly one accepted delivery ticket" in prompt
                    for prompt in second_dispatcher.prompts
                )
            )

    def test_crash_matrix_converges_before_and_after_each_delivery_boundary(self) -> None:
        boundaries = (
            "tracker:publish",
            "git:worktree:integration",
            "git:worktree:ticket:01",
            "ticket:accept:01",
            "git:merge:01",
            "tracker:close:01",
            "review:accept:1",
            "result:publish",
        )

        @dataclass
        class DeliveryCase:
            config: WorkflowConfig
            goal: Path
            external: dict[str, object] = field(default_factory=dict)
            dispatcher: CompleteDispatcher = field(default_factory=CompleteDispatcher)
            result: DeliveryResult | None = None

        def setup(root: Path) -> DeliveryCase:
            repository = root / "repository"
            _initialize_repository(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            return DeliveryCase(config=_workflow(repository), goal=goal)

        def execute(case: DeliveryCase) -> None:
            case.result = StandardizedDelivery(
                case.config,
                dispatcher=case.dispatcher,
                tracker=StableTracker(case.external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(case.goal)

        def run(case: DeliveryCase, boundary: str | None) -> None:
            if boundary is None:
                execute(case)
                return
            transition, operation_key = boundary.split("/", 1)
            interrupted = [False]
            interrupt = _interrupt_journal(
                DeliveryJournal._persist_event,
                transition,
                operation_key,
                interrupted,
            )
            with patch.object(DeliveryJournal, "_persist_event", interrupt):
                try:
                    execute(case)
                except RuntimeError as exc:
                    if not interrupted[0] or str(exc) != "journal interruption":
                        raise
                    raise CrashInjected(boundary) from exc

        def observe(case: DeliveryCase) -> dict[str, object]:
            result = case.result
            assert result is not None
            integration = result.artifact_root / "worktrees/integration"
            subjects = _git(integration, "log", "--format=%s").stdout
            return {
                "status": result.status,
                "tracker": dict(case.external),
                "merge_count": subjects.count("Merge branch"),
                "ticket_commit_count": subjects.count("feat: implement journal slice"),
                "business_artifact": (integration / "slice-01.txt").read_bytes(),
                "turn_count": len(case.dispatcher.prompts),
            }

        results = run_public_operation_crash_matrix(
            (
                f"{transition}/{key}"
                for key in boundaries
                for transition in ("effect_intent", "effect_confirmed")
            ),
            setup=setup,
            run=run,
            restart=execute,
            observe=observe,
        )
        self.assertEqual(len(results), 16)
        for result in results.values():
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["tracker"]["publish_mutations"], 1)
            self.assertEqual(result["tracker"]["close_mutations"], 1)
            self.assertEqual(result["merge_count"], 1)

    def test_applied_git_and_review_effects_converge_without_confirmation(
        self,
    ) -> None:
        targets = (
            "git:worktree:integration",
            "git:worktree:ticket:01",
            "git:merge:01",
            "review:accept:1",
        )
        for target in targets:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary).resolve() / "repository"
                _initialize_repository(repository)
                config = _workflow(repository)
                goal = repository / "goal.md"
                goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
                external: dict[str, object] = {}
                interrupted = [False]
                interrupt = _interrupt_before_confirmation(
                    DeliveryJournal._persist_event,
                    target,
                    interrupted,
                )

                with (
                    patch.object(DeliveryJournal, "_persist_event", interrupt),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "applied effect before confirmation",
                    ),
                ):
                    StandardizedDelivery(
                        config,
                        dispatcher=CompleteDispatcher(),
                        tracker=StableTracker(external),
                        controller_harness=Harness.DROID,
                        worker_harnesses=(Harness.DROID,),
                    ).run(goal)
                self.assertTrue(interrupted[0])
                run_root = next(config.standardized_delivery.artifact_root.iterdir())
                events = [
                    json.loads(line)
                    for line in (run_root / "journal.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertFalse(
                    any(
                        event["event"] == "effect_confirmed" and event["operation_key"] == target
                        for event in events
                    )
                )
                if target == "git:worktree:integration":
                    self.assertTrue((run_root / "worktrees/integration").is_dir())
                elif target == "git:worktree:ticket:01":
                    self.assertTrue((run_root / "worktrees/ticket-01").is_dir())
                elif target == "git:merge:01":
                    log = _git(
                        run_root / "worktrees/integration",
                        "log",
                        "--format=%s",
                    ).stdout
                    self.assertEqual(log.count("Merge branch"), 1)
                else:
                    self.assertTrue((run_root / "reviews/round-1/standards.json").is_file())
                    self.assertTrue((run_root / "reviews/round-1/spec.json").is_file())

                result = StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
                recovered_events = [
                    json.loads(line)
                    for line in (run_root / "journal.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]

                self.assertEqual(result.status, "succeeded")
                self.assertEqual(
                    sum(
                        event["event"] == "effect_confirmed" and event["operation_key"] == target
                        for event in recovered_events
                    ),
                    1,
                )
                final_log = _git(
                    result.artifact_root / "worktrees/integration",
                    "log",
                    "--format=%s",
                ).stdout
                self.assertEqual(final_log.count("Merge branch"), 1)

    def test_human_commit_after_confirmed_merge_stops_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            original = DeliveryJournal._persist_event

            def interrupt(
                journal: DeliveryJournal,
                event: str,
                operation_key: str | None,
                effect_kind: str | None,
                details: dict[str, object],
                observed_at: float,
            ) -> None:
                original(
                    journal,
                    event,
                    operation_key,
                    effect_kind,
                    details,
                    observed_at,
                )
                if event == "effect_confirmed" and operation_key == "git:merge:01":
                    raise RuntimeError("crash after confirmed merge")

            with (
                patch.object(DeliveryJournal, "_persist_event", interrupt),
                self.assertRaisesRegex(RuntimeError, "crash after confirmed merge"),
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            integration = run_root / "worktrees/integration"
            marker = integration / "human.txt"
            marker.write_text("concurrent human change\n", encoding="utf-8")
            _git(integration, "add", marker.name)
            _git(integration, "commit", "-m", "feat: concurrent human change")

            with self.assertRaisesRegex(
                DeliveryError,
                "delivery_recovery_conflict:git.integration",
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)

    def test_completed_result_rejects_missing_confirmed_tracker_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
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
            self.assertEqual(result.status, "succeeded")
            external["published"] = False

            with self.assertRaisesRegex(
                DeliveryError,
                "delivery_recovery_conflict:tracker.publish",
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)


if __name__ == "__main__":
    unittest.main()
