from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_delivery_journal import CompleteDispatcher, _initialize_repository, _workflow

import herdr_orchestrator.delivery as delivery_module
from herdr_orchestrator.delivery import DeliveryError, StandardizedDelivery
from herdr_orchestrator.delivery_journal import DeliveryJournal
from herdr_orchestrator.model import Harness


class ProjectionInterrupted(BaseException):
    pass


class DeliveryStageJournalTests(unittest.TestCase):
    def test_process_death_at_stage_boundaries_recovers_only_durable_history(self) -> None:
        for boundary in ("before_append", "after_append", "after_projection"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                process = multiprocessing.get_context("spawn").Process(
                    target=_crash_stage_transition,
                    args=(root, boundary),
                )
                process.start()
                process.join(timeout=15)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
                    self.fail("stage crash process did not terminate")
                self.assertEqual(process.exitcode, 73)
                before = (root / "journal.jsonl").read_bytes()
                expected = "wayfinder" if boundary == "before_append" else "spec-and-tickets"
                for _ in range(2):
                    with DeliveryJournal.claim(
                        root, "run", 10, clock=lambda: 20, error_type=DeliveryError
                    ) as journal:
                        delivery = StandardizedDelivery.__new__(StandardizedDelivery)
                        delivery._run_root = root
                        delivery._journal = journal
                        delivery._previous_state = {}
                        with (
                            patch.object(
                                delivery_module,
                                "_load_completed_result",
                                side_effect=ProjectionInterrupted,
                            ),
                            self.assertRaises(ProjectionInterrupted),
                        ):
                            delivery._run_claimed("run")
                        self.assertEqual(
                            json.loads((root / "state.json").read_text())["stage"], expected
                        )
                self.assertTrue((root / "journal.jsonl").read_bytes().startswith(before))
                stages = [
                    event["details"]["stage"]
                    for event in _events(root)
                    if event["event"] == "stage_transition"
                ]
                self.assertEqual(
                    stages,
                    (
                        ["wayfinder"]
                        if boundary == "before_append"
                        else ["wayfinder", "spec-and-tickets"]
                    ),
                )
                self.assertFalse(
                    any(event["event"].startswith("effect_") for event in _events(root))
                )

    def test_stage_append_precedes_projection_and_recovery_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            delivery = StandardizedDelivery.__new__(StandardizedDelivery)
            delivery._run_root = root
            delivery._previous_state = {}
            state_path = root / "state.json"
            with DeliveryJournal.claim(root, "run", 60, error_type=DeliveryError) as journal:
                delivery._journal = journal
                delivery._write_state("running", stage="wayfinder", controller="droid")
                before = state_path.read_bytes()
                with (
                    patch.object(delivery_module, "_write_json", side_effect=ProjectionInterrupted),
                    self.assertRaises(ProjectionInterrupted),
                ):
                    delivery._write_state("running", stage="spec-and-tickets")
                self.assertEqual(state_path.read_bytes(), before)
                self.assertEqual(journal.latest_stage_state()["stage"], "spec-and-tickets")
            for _ in range(2):
                with DeliveryJournal.claim(root, "run", 60, error_type=DeliveryError) as journal:
                    delivery._journal = journal
                    with patch.object(delivery_module, "_load_completed_result") as load:
                        load.side_effect = ProjectionInterrupted
                        with self.assertRaises(ProjectionInterrupted):
                            delivery._run_claimed("run")
                    self.assertEqual(
                        json.loads(state_path.read_text())["stage"], "spec-and-tickets"
                    )
                    delivery._write_state("running", stage="spec-and-tickets")
            events = _events(root)
            stages = [
                event["details"]["stage"]
                for event in events
                if event["event"] == "stage_transition"
            ]
            self.assertEqual(stages, ["wayfinder", "spec-and-tickets"])
            self.assertEqual(
                [event["sequence"] for event in events], list(range(1, len(events) + 1))
            )
            self.assertFalse(any(event["event"].startswith("effect_") for event in events))

    def test_stage_write_requires_owner_and_never_changes_projection_on_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            delivery = StandardizedDelivery.__new__(StandardizedDelivery)
            delivery._run_root = root
            delivery._previous_state = {}
            delivery._journal = None
            with self.assertRaisesRegex(DeliveryError, "delivery_journal_unavailable"):
                delivery._write_state("failed", stage="stopped", error="RuntimeError")
            self.assertFalse((root / "state.json").exists())
            with DeliveryJournal.claim(root, "run", 60, error_type=DeliveryError) as journal:
                delivery._journal = journal
                delivery._write_state("running", stage="wayfinder")
                before = (root / "state.json").read_bytes()
                journal_before = (root / "journal.jsonl").read_bytes()
                token = journal.owner_token
                journal.owner_token = "f" * 32
                try:
                    with self.assertRaisesRegex(DeliveryError, "delivery_owner_lost"):
                        delivery._write_state("running", stage="implementation")
                    self.assertEqual((root / "state.json").read_bytes(), before)
                    self.assertEqual((root / "journal.jsonl").read_bytes(), journal_before)
                finally:
                    journal.owner_token = token
            with self.assertRaisesRegex(DeliveryError, "delivery_owner_lost"):
                journal.record_stage({"status": "running", "stage": "wayfinder"})

    def test_invalid_and_sensitive_stage_records_are_rejected_before_append(self) -> None:
        cases = [
            {},
            None,
            {"status": "failed", "stage": "stopped", "error": "\ud800"},
            {"status": "running", "stage": "unknown"},
            {"status": "succeeded", "stage": "implementation"},
            {"status": "running", "stage": "wayfinder", "prompt": "private prompt"},
            {"status": "running", "stage": "wayfinder", "controller": ["droid"]},
            {"status": "failed", "stage": "stopped", "error": "x" * 1025},
            {"status": "failed", "stage": "stopped", "error": "sk-" + "a" * 30},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with DeliveryJournal.claim(root, "run", 60, error_type=DeliveryError) as journal:
                before = (root / "journal.jsonl").read_bytes()
                for state in cases:
                    with (
                        self.subTest(state=state),
                        self.assertRaisesRegex(
                            DeliveryError, "delivery_journal_(stage_invalid|sensitive_value)"
                        ),
                    ):
                        journal.record_stage(state)
                    self.assertEqual((root / "journal.jsonl").read_bytes(), before)

    def test_loader_rejects_forged_stage_owner_and_malformed_stage_payload(self) -> None:
        for mutation in ("owner", "stage", "secret", "operation_key", "released"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                with DeliveryJournal.claim(root, "run", 60, error_type=DeliveryError) as journal:
                    journal.record_stage({"status": "running", "stage": "wayfinder"})
                events = _events(root)
                stage = events[1]
                if mutation == "owner":
                    stage["owner_token"] = "f" * 32
                elif mutation == "stage":
                    stage["details"]["stage"] = "unknown"
                elif mutation == "secret":
                    stage["details"]["error"] = "sk-" + "a" * 30
                elif mutation == "operation_key":
                    stage["operation_key"] = "stage:wayfinder"
                else:
                    events[1], events[2] = events[2], events[1]
                    for index, event in enumerate(events, 1):
                        event["sequence"] = index
                (root / "journal.jsonl").write_text(
                    "".join(json.dumps(event) + "\n" for event in events)
                )
                with self.assertRaisesRegex(DeliveryError, "delivery_journal_"):
                    DeliveryJournal(root, "run", "e" * 32, 60, error_type=DeliveryError)

    def test_completed_run_repairs_stage_projection_without_replaying_external_actions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.")
            dispatcher = CompleteDispatcher()
            delivery = StandardizedDelivery(
                config,
                dispatcher=dispatcher,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            result = delivery.run(goal)
            before = [
                row
                for row in _events(result.artifact_root)
                if not row["event"].startswith("owner_")
            ]
            self.assertEqual(
                [row["details"]["stage"] for row in before if row["event"] == "stage_transition"],
                [
                    "wayfinder",
                    "spec-and-tickets",
                    "tracker-publish",
                    "implementation",
                    "final-review",
                    "complete",
                ],
            )
            for damaged in (
                None,
                "invalid JSON",
                json.dumps({"status": "running", "controller": "unknown"}),
            ):
                state_path = result.artifact_root / "state.json"
                if damaged is None:
                    state_path.unlink()
                else:
                    state_path.write_text(damaged)
                with patch.object(
                    dispatcher, "dispatch", side_effect=AssertionError("unexpected dispatch")
                ):
                    self.assertEqual(delivery.run(goal), result)
                self.assertEqual(json.loads(state_path.read_text())["stage"], "complete")
                after = [
                    row
                    for row in _events(result.artifact_root)
                    if not row["event"].startswith("owner_")
                ]
                self.assertEqual(after, before)

    def test_legacy_journal_without_stages_stays_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with DeliveryJournal.claim(root, "run", 60, error_type=DeliveryError):
                pass
            original = (root / "journal.jsonl").read_bytes()
            with DeliveryJournal.claim(root, "run", 60, error_type=DeliveryError) as journal:
                self.assertIsNone(journal.latest_stage_state())
                delivery = StandardizedDelivery.__new__(StandardizedDelivery)
                delivery._run_root = root
                delivery._journal = journal
                delivery._previous_state = {
                    "status": "running",
                    "stage": "final-review",
                    "controller": "droid",
                }
                delivery._write_state("failed", stage="stopped", error="RuntimeError")
                self.assertEqual(journal.latest_stage_state()["controller"], "droid")
            self.assertTrue((root / "journal.jsonl").read_bytes().startswith(original))


def _events(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / "journal.jsonl").read_text().splitlines()]


def _crash_stage_transition(root: Path, boundary: str) -> None:
    delivery = StandardizedDelivery.__new__(StandardizedDelivery)
    delivery._run_root = root
    delivery._previous_state = {}
    with DeliveryJournal.claim(root, "run", 10, clock=lambda: 0) as journal:
        delivery._journal = journal
        delivery._write_state("running", stage="wayfinder", controller="droid")
        persist = DeliveryJournal._persist_event
        write = delivery_module._write_json

        def interrupt_append(self, event, key, kind, details, observed_at):
            if event == "stage_transition" and boundary == "before_append":
                os._exit(73)
            persist(self, event, key, kind, details, observed_at)
            if event == "stage_transition" and boundary == "after_append":
                os._exit(73)

        def interrupt_projection(path, state):
            write(path, state)
            if boundary == "after_projection":
                os._exit(73)

        with (
            patch.object(DeliveryJournal, "_persist_event", interrupt_append),
            patch.object(delivery_module, "_write_json", interrupt_projection),
        ):
            delivery._write_state("running", stage="spec-and-tickets")
