from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
)
from herdr_orchestrator.store import Store, StoreError


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.db")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_enqueue_is_idempotent(self) -> None:
        job = _job("same")

        first_id, first_created = self.store.enqueue(job)
        second_id, second_created = self.store.enqueue(job)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        self.assertEqual(
            self.store.existing_job("example", "same"),
            (first_id, Harness.CODEX),
        )

    def test_claims_only_one_job_per_harness(self) -> None:
        self.store.enqueue(_job("one", Harness.CODEX))
        self.store.enqueue(_job("two", Harness.CODEX))
        self.store.enqueue(_job("three", Harness.DROID))

        claimed = self.store.claim("example", limit=3, lease_seconds=60)

        self.assertEqual(len(claimed), 2)
        self.assertEqual({job.harness for job in claimed}, {Harness.CODEX, Harness.DROID})
        self.assertEqual({job.agent_name for job in claimed}, {"ho-codex", "ho-droid"})

    def test_claims_up_to_harness_replica_slots(self) -> None:
        self.store.enqueue(_job("one", Harness.GROK))
        self.store.enqueue(_job("two", Harness.GROK))
        self.store.enqueue(_job("three", Harness.GROK))

        claimed = self.store.claim(
            "example",
            limit=5,
            lease_seconds=60,
            slot_names={"grok": ("ho-grok-01-slot", "ho-grok-02-slot")},
        )

        self.assertEqual(len(claimed), 2)
        self.assertEqual(
            [job.agent_name for job in claimed],
            ["ho-grok-01-slot", "ho-grok-02-slot"],
        )

    def test_claim_respects_runtime_worker_pool(self) -> None:
        self.store.enqueue(_job("codex", Harness.CODEX))
        self.store.enqueue(_job("grok", Harness.GROK))

        claimed = self.store.claim(
            "example",
            limit=2,
            lease_seconds=60,
            allowed_harnesses=(Harness.GROK,),
        )

        self.assertEqual([job.harness for job in claimed], [Harness.GROK])
        self.assertEqual(self.store.status_counts("example")["pending"], 1)

    def test_success_records_receipt_and_terminal_state(self) -> None:
        self.store.enqueue(_job("one"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        state = self.store.record_outcome(
            claimed,
            DispatchOutcome(
                "worker",
                AgentState.DONE,
                False,
                "w2:p2",
                placement=PlacementTarget.WORKTREE,
                execution_path="/repo/.orchestrator/worktrees/task",
                herdr_workspace_id="w2",
            ),
        )
        job = self.store.jobs("example")[0]
        with sqlite3.connect(self.store.path) as connection:
            receipt = connection.execute(
                """
                SELECT placement, execution_path, herdr_workspace_id
                FROM receipts
                """
            ).fetchone()

        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertEqual(self.store.status_counts("example")["succeeded"], 1)
        self.assertEqual(
            job["execution_path"],
            "/repo/.orchestrator/worktrees/task",
        )
        self.assertEqual(job["herdr_workspace_id"], "w2")
        self.assertEqual(
            receipt,
            (
                PlacementTarget.WORKTREE.value,
                "/repo/.orchestrator/worktrees/task",
                "w2",
            ),
        )

    def test_declared_task_receipt_is_claimed_and_verification_is_recorded(self) -> None:
        self.store.enqueue(
            NewJob(
                workflow="example",
                title="verified",
                harness=Harness.PI,
                prompt="Inspect the repository",
                dedupe_key="verified-v1",
                max_attempts=2,
                receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, "MOCK-OK harness=pi"),
            )
        )

        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        state = self.store.record_outcome(
            claimed,
            DispatchOutcome(
                "worker",
                AgentState.DONE,
                False,
                "pane",
                task_verified=True,
            ),
        )
        job = self.store.jobs("example")[0]

        self.assertEqual(
            claimed.receipt,
            TaskReceipt(ReceiptKind.OUTPUT_PREFIX, "MOCK-OK harness=pi"),
        )
        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertIs(job["agent_settled"], True)
        self.assertIs(job["task_verified"], True)

    def test_declared_task_receipt_fails_closed_when_verification_is_unreported(self) -> None:
        self.store.enqueue(
            NewJob(
                workflow="example",
                title="unverified",
                harness=Harness.PI,
                prompt="Inspect the repository",
                dedupe_key="unverified-v1",
                max_attempts=1,
                receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, "MOCK-OK harness=pi"),
            )
        )
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        state = self.store.record_outcome(
            claimed,
            DispatchOutcome(
                "worker",
                AgentState.DONE,
                False,
                "pane",
            ),
        )
        job = self.store.jobs("example")[0]

        self.assertEqual(state, JobState.FAILED)
        self.assertEqual(job["error_code"], "task_receipt_missing")
        self.assertIs(job["agent_settled"], True)
        self.assertIsNone(job["task_verified"])

    def test_agent_blocked_error_is_terminal_blocked_state(self) -> None:
        self.store.enqueue(_job("one"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        state = self.store.record_outcome(
            claimed,
            DispatchOutcome(
                "worker",
                AgentState.BLOCKED,
                True,
                "w1:p2",
                "agent_blocked",
            ),
        )

        self.assertEqual(state, JobState.BLOCKED)
        self.assertEqual(self.store.status_counts("example")["blocked"], 1)

    def test_failure_retries_then_exhausts_attempts(self) -> None:
        self.store.enqueue(_job("one", max_attempts=2))
        first = self.store.claim("example", limit=1, lease_seconds=60)[0]
        state = self.store.record_outcome(
            first,
            DispatchOutcome(
                "worker",
                AgentState.UNKNOWN,
                False,
                None,
                "herdr_timeout",
                error_summary="Provider request timed out after 30 seconds",
            ),
        )
        self.assertEqual(state, JobState.PENDING)
        self.assertEqual(
            self.store.jobs("example")[0]["error_summary"],
            "Provider request timed out after 30 seconds",
        )

        with patch("herdr_orchestrator.store.time.time", return_value=time.time() + 120):
            second = self.store.claim("example", limit=1, lease_seconds=60)[0]
            state = self.store.record_outcome(
                second,
                DispatchOutcome(
                    "worker",
                    AgentState.UNKNOWN,
                    True,
                    "w1:p2",
                    "herdr_timeout",
                ),
            )

        self.assertEqual(state, JobState.FAILED)

    def test_failed_job_can_be_retried_with_additional_attempt_budget(self) -> None:
        job_id, _ = self.store.enqueue(_job("retry", max_attempts=1))
        failed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        self.store.record_outcome(
            failed,
            DispatchOutcome(
                "worker",
                AgentState.UNKNOWN,
                False,
                None,
                "agent_provider_failed",
            ),
        )

        retried = self.store.retry_failed(
            "example",
            job_id,
            extra_attempts=2,
        )
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        self.assertEqual(retried["state"], JobState.PENDING.value)
        self.assertEqual(claimed.attempt, 2)
        self.assertEqual(claimed.max_attempts, 3)
        with self.assertRaisesRegex(StoreError, "job_not_retryable"):
            self.store.retry_failed("example", job_id, extra_attempts=1)

    def test_expired_lease_is_reclaimed(self) -> None:
        self.store.enqueue(_job("one"))
        baseline = time.time() + 1
        with patch("herdr_orchestrator.store.time.time", return_value=baseline):
            first = self.store.claim("example", limit=1, lease_seconds=30)[0]
        with patch("herdr_orchestrator.store.time.time", return_value=baseline + 31):
            second = self.store.claim("example", limit=1, lease_seconds=30)[0]

        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(second.attempt, 2)

    def test_migrates_v1_jobs_and_receipts_to_current_schema(self) -> None:
        path = Path(self.temporary.name) / "v1.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE schema_meta (version INTEGER NOT NULL);
            INSERT INTO schema_meta(version) VALUES (1);
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow TEXT NOT NULL,
                title TEXT NOT NULL,
                harness TEXT NOT NULL,
                prompt TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                available_at REAL NOT NULL,
                lease_until REAL,
                agent_name TEXT,
                error_code TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(workflow, dedupe_key)
            );
            CREATE TABLE receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id),
                attempt INTEGER NOT NULL,
                state TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                agent_state TEXT NOT NULL,
                member_reused INTEGER NOT NULL,
                pane_id TEXT,
                error_code TEXT,
                observed_at REAL NOT NULL
            );
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO jobs(
                workflow, title, harness, prompt, dedupe_key, state,
                attempts, max_attempts, available_at, created_at, updated_at
            ) VALUES (
                'example', 'old', 'codex', 'inspect', 'old-v1', 'pending',
                0, 2, 1, 1, 1
            );
            """
        )
        connection.commit()
        connection.close()

        store = Store(path)
        store.initialize()

        migrated = store.jobs("example")
        with sqlite3.connect(path) as migrated_connection:
            version = migrated_connection.execute(
                "SELECT version FROM schema_meta"
            ).fetchone()[0]
            job_columns = {
                row[1]
                for row in migrated_connection.execute("PRAGMA table_info(jobs)")
            }
            receipt_columns = {
                row[1]
                for row in migrated_connection.execute("PRAGMA table_info(receipts)")
            }

        self.assertEqual(version, 3)
        self.assertEqual(migrated[0]["placement"], PlacementTarget.TAB.value)
        self.assertIsNone(migrated[0]["task_verified"])
        self.assertIsNone(migrated[0]["agent_settled"])
        self.assertIn("execution_path", job_columns)
        self.assertIn("herdr_workspace_id", job_columns)
        self.assertIn("receipt_kind", job_columns)
        self.assertIn("receipt_value", job_columns)
        self.assertIn("agent_settled", job_columns)
        self.assertIn("task_verified", job_columns)
        self.assertIn("error_summary", job_columns)
        self.assertIn("execution_path", receipt_columns)
        self.assertIn("herdr_workspace_id", receipt_columns)
        self.assertIn("agent_settled", receipt_columns)
        self.assertIn("task_verified", receipt_columns)
        self.assertIn("error_summary", receipt_columns)


def _job(
    dedupe_key: str,
    harness: Harness = Harness.CODEX,
    *,
    max_attempts: int = 3,
) -> NewJob:
    return NewJob(
        workflow="example",
        title=dedupe_key,
        harness=harness,
        prompt="Do the task",
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )


if __name__ == "__main__":
    unittest.main()
