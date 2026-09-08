from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.model import AgentState, DispatchOutcome, Harness, NewJob, PlacementTarget
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.store import Store, StoreError

REPO_ROOT = Path(__file__).resolve().parents[1]


class QueueOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "state.db")
        self.store.initialize()

    def test_blocked_and_resuming_jobs_keep_their_replica_slot(self) -> None:
        self.store.enqueue(self._job("blocked"))
        job = self.store.claim("queue", limit=1, lease_seconds=60)[0]
        self.store.record_outcome(
            job,
            DispatchOutcome(job.agent_name, AgentState.BLOCKED, False, "w1:p2", "agent_blocked"),
        )
        self.store.enqueue(self._job("pending"))

        for resuming in (False, True):
            with self.subTest(resuming=resuming):
                if resuming:
                    self.store.claim_blocked_for_resume("queue", job.job_id, lease_seconds=60)
                self.assertEqual(self.store.claim("queue", limit=1, lease_seconds=60), [])
                pending = next(row for row in self.store.jobs("queue") if row["title"] == "pending")
                self.assertEqual(pending["attempts"], 0)
                self.assertEqual(pending["state"], "pending")

    def test_blocked_slot_leaves_other_replicas_available(self) -> None:
        slots = {"codex": ("slot-1", "slot-2")}
        self.store.enqueue(self._job("blocked"))
        job = self.store.claim("queue", limit=1, lease_seconds=60, slot_names=slots)[0]
        self.store.record_outcome(
            job,
            DispatchOutcome(job.agent_name, AgentState.BLOCKED, False, "w1:p2", "agent_blocked"),
        )
        self.store.enqueue(self._job("pending"))
        claimed = self.store.claim("queue", limit=2, lease_seconds=60, slot_names=slots)
        self.assertEqual([item.agent_name for item in claimed], ["slot-2"])

    def test_blocked_job_reserves_capacity_across_placements(self) -> None:
        self.store.enqueue(self._job("blocked"))
        job = self.store.claim("queue", limit=1, lease_seconds=60)[0]
        self.store.record_outcome(
            job,
            DispatchOutcome(job.agent_name, AgentState.BLOCKED, False, "w1:p2", "agent_blocked"),
        )
        for placement in (PlacementTarget.PANE, PlacementTarget.WORKTREE):
            self.store.enqueue(replace(self._job(placement.value), placement=placement))
        self.assertEqual(self.store.claim("queue", limit=2, lease_seconds=60), [])
        pending = [row for row in self.store.jobs("queue") if row["state"] == "pending"]
        self.assertEqual([row["attempts"] for row in pending], [0, 0])

    def test_expired_attempt_is_recovered_before_older_pending_work(self) -> None:
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            older_id, _ = self.store.enqueue(self._job("older-pending"))
            self.store.enqueue(self._job("running"))
            with closing(sqlite3.connect(self.store.path)) as connection, connection:
                connection.execute("UPDATE jobs SET available_at = 120 WHERE id = ?", (older_id,))
            original = self.store.claim("queue", limit=1, lease_seconds=10)[0]

        with patch("herdr_orchestrator.store.time.time", return_value=130.0):
            recovered = self.store.claim("queue", limit=1, lease_seconds=60)[0]

        self.assertEqual(recovered.job_id, original.job_id)
        self.assertEqual(recovered.attempt_id, original.attempt_id)
        self.assertTrue(recovered.recovery)
        self.assertEqual(self.store.jobs("queue")[0]["attempts"], 0)

    def test_outcome_and_health_failures_do_not_discard_sibling_results(self) -> None:
        base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        for failing_stage in ("outcome", "health"):
            with self.subTest(failing_stage=failing_stage):
                config = replace(base, name=failing_stage, state_db=self.store.path)
                for harness in (Harness.CODEX, Harness.DROID):
                    self.store.enqueue(
                        replace(self._job(harness.value, harness), workflow=config.name)
                    )
                coordinator = Coordinator(config, store=self.store, dispatcher=_Dispatcher())
                original = self.store.record_outcome

                def record_outcome(job, outcome, stage=failing_stage, record=original):
                    if stage == "outcome" and job.harness is Harness.CODEX:
                        raise StoreError("job_lease_lost")
                    return record(job, outcome)

                def record_health(harness, outcome, stage=failing_stage):
                    if stage == "health" and harness is Harness.CODEX:
                        raise StoreError("health_write_failed")

                with (
                    patch.object(self.store, "record_outcome", side_effect=record_outcome),
                    patch.object(coordinator, "_record_health", side_effect=record_health),
                    patch(
                        "herdr_orchestrator.runner.as_completed",
                        side_effect=lambda futures: iter(futures),
                    ),
                    self.assertRaisesRegex(StoreError, "job_lease_lost|health_write_failed"),
                ):
                    coordinator.run_once()

                sibling = next(
                    row for row in self.store.jobs(config.name) if row["harness"] == "droid"
                )
                self.assertEqual(sibling["state"], "succeeded")
                self.assertEqual(sibling["attempts"], 1)
                with closing(sqlite3.connect(self.store.path)) as connection:
                    receipt = connection.execute(
                        "SELECT state FROM receipts WHERE job_id = ?", (sibling["id"],)
                    ).fetchone()
                self.assertEqual(receipt, ("succeeded",))

    def test_recovery_respects_a_reduced_replica_limit(self) -> None:
        slots = {"codex": ("slot-1", "slot-2")}
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            for title in ("first", "second"):
                self.store.enqueue(self._job(title))
            originals = self.store.claim("queue", limit=2, lease_seconds=10, slot_names=slots)
        with patch("herdr_orchestrator.store.time.time", return_value=120.0):
            recovered = self.store.claim(
                "queue",
                limit=2,
                lease_seconds=60,
                slot_names=slots,
                slot_limits={"codex": 1},
            )
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0].recovery)
        self.assertEqual(recovered[0].attempt_id, originals[0].attempt_id)

    def test_recovery_does_not_reuse_an_agent_owned_by_another_live_job(self) -> None:
        slots = {"codex": ("slot-1", "slot-2")}
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            for title in ("first", "second"):
                self.store.enqueue(self._job(title))
            originals = self.store.claim("queue", limit=2, lease_seconds=10, slot_names=slots)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                "UPDATE jobs SET agent_name = ?, lease_until = 200 WHERE id = ?",
                (originals[0].agent_name, originals[1].job_id),
            )
        with patch("herdr_orchestrator.store.time.time", return_value=120.0):
            self.assertEqual(
                self.store.claim("queue", limit=2, lease_seconds=60, slot_names=slots),
                [],
            )

    @staticmethod
    def _job(title: str, harness: Harness = Harness.CODEX) -> NewJob:
        return NewJob(
            workflow="queue",
            title=title,
            harness=harness,
            prompt="Read only.",
            dedupe_key=title,
            max_attempts=2,
        )


class _Dispatcher:
    def dispatch(self, harness, prompt, *, timeout_seconds, agent_name=None, context=None):
        return DispatchOutcome(agent_name, AgentState.DONE, False, f"w1:{harness.value}")


if __name__ == "__main__":
    unittest.main()
