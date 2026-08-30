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
    HarnessHealthStatus,
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
            receipt = connection.execute("""
                SELECT placement, execution_path, herdr_workspace_id, correlation_id
                FROM receipts
                """).fetchone()

        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertEqual(self.store.status_counts("example")["succeeded"], 1)
        self.assertEqual(
            job["execution_path"],
            "/repo/.orchestrator/worktrees/task",
        )
        self.assertEqual(job["herdr_workspace_id"], "w2")
        self.assertEqual(job["correlation_id"], claimed.correlation_id)
        self.assertEqual(
            receipt,
            (
                PlacementTarget.WORKTREE.value,
                "/repo/.orchestrator/worktrees/task",
                "w2",
                claimed.correlation_id,
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

    def test_blocked_job_can_resume_same_attempt_and_record_success(self) -> None:
        job_id, _ = self.store.enqueue(_job("resume", max_attempts=1))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        self.store.record_outcome(
            claimed,
            DispatchOutcome(
                "worker",
                AgentState.BLOCKED,
                False,
                "w1:p2",
                "agent_blocked",
            ),
        )

        blocked, pane_id = self.store.claim_blocked_for_resume(
            "example",
            job_id,
            lease_seconds=60,
        )
        state = self.store.record_resume_outcome(
            blocked,
            DispatchOutcome(
                "worker",
                AgentState.DONE,
                True,
                "w1:p2",
            ),
        )
        job = self.store.jobs("example")[0]
        with sqlite3.connect(self.store.path) as connection:
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]

        self.assertEqual(pane_id, "w1:p2")
        self.assertEqual(blocked.attempt, 1)
        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(receipt_count, 2)

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

    def test_harness_health_persists_by_workflow_workspace_and_harness(self) -> None:
        self.store.record_harness_health(
            "example",
            "/repo",
            Harness.GROK,
            status=HarnessHealthStatus.READY,
            reason_code=None,
            source="doctor",
            observed_at=100.0,
            expires_at=1900.0,
            cooldown_until=100.0,
            consecutive_failures=0,
        )

        health = self.store.harness_health(
            "example",
            "/repo",
            (Harness.GROK, Harness.CODEX),
        )

        self.assertEqual(
            health[Harness.GROK],
            {
                "status": "ready",
                "reason_code": None,
                "source": "doctor",
                "observed_at": 100.0,
                "expires_at": 1900.0,
                "cooldown_until": 100.0,
                "consecutive_failures": 0,
                "probe_lease_until": None,
            },
        )
        self.assertNotIn(Harness.CODEX, health)

    def test_harness_probe_lease_deduplicates_concurrent_refreshes(self) -> None:
        first = self.store.claim_harness_probe(
            "example",
            "/repo",
            Harness.GROK,
            now=100.0,
            lease_seconds=40,
            probe_token="first",
        )
        concurrent = self.store.claim_harness_probe(
            "example",
            "/repo",
            Harness.GROK,
            now=101.0,
            lease_seconds=40,
            probe_token="concurrent",
        )
        expired = self.store.claim_harness_probe(
            "example",
            "/repo",
            Harness.GROK,
            now=141.0,
            lease_seconds=40,
            probe_token="successor",
        )

        self.assertTrue(first)
        self.assertFalse(concurrent)
        self.assertTrue(expired)

    def test_stale_probe_owner_cannot_release_or_overwrite_successor(self) -> None:
        self.assertTrue(
            self.store.claim_harness_probe(
                "example",
                "/repo",
                Harness.GROK,
                now=100.0,
                lease_seconds=10,
                probe_token="stale",
            )
        )
        self.assertTrue(
            self.store.claim_harness_probe(
                "example",
                "/repo",
                Harness.GROK,
                now=111.0,
                lease_seconds=10,
                probe_token="successor",
            )
        )

        stale_release = self.store.release_harness_probe(
            "example",
            "/repo",
            Harness.GROK,
            probe_token="stale",
        )
        stale_record = self.store.record_harness_health(
            "example",
            "/repo",
            Harness.GROK,
            status=HarnessHealthStatus.DEGRADED,
            reason_code="herdr_timeout",
            source="preflight",
            observed_at=112.0,
            expires_at=112.0,
            cooldown_until=412.0,
            consecutive_failures=1,
            probe_token="stale",
        )
        successor_record = self.store.record_harness_health(
            "example",
            "/repo",
            Harness.GROK,
            status=HarnessHealthStatus.READY,
            reason_code=None,
            source="preflight",
            observed_at=113.0,
            expires_at=1913.0,
            cooldown_until=113.0,
            consecutive_failures=0,
            probe_token="successor",
        )
        health = self.store.harness_health(
            "example",
            "/repo",
            (Harness.GROK,),
        )[Harness.GROK]

        self.assertFalse(stale_release)
        self.assertFalse(stale_record)
        self.assertTrue(successor_record)
        self.assertEqual(health["status"], "ready")
        self.assertIsNone(health["probe_lease_until"])

    def test_migrates_v1_jobs_and_receipts_to_current_schema(self) -> None:
        path = Path(self.temporary.name) / "v1.db"
        connection = sqlite3.connect(path)
        connection.executescript("""
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
            """)
        connection.commit()
        connection.close()

        store = Store(path)
        store.initialize()

        migrated = store.jobs("example")
        with sqlite3.connect(path) as migrated_connection:
            version = migrated_connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            job_columns = {row[1] for row in migrated_connection.execute("PRAGMA table_info(jobs)")}
            receipt_columns = {
                row[1] for row in migrated_connection.execute("PRAGMA table_info(receipts)")
            }
            health_columns = {
                row[1] for row in migrated_connection.execute("PRAGMA table_info(harness_health)")
            }

        self.assertEqual(version, 5)
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
        self.assertIn("correlation_id", job_columns)
        self.assertIn("execution_path", receipt_columns)
        self.assertIn("herdr_workspace_id", receipt_columns)
        self.assertIn("agent_settled", receipt_columns)
        self.assertIn("task_verified", receipt_columns)
        self.assertIn("error_summary", receipt_columns)
        self.assertIn("correlation_id", receipt_columns)
        self.assertIn("probe_lease_token", health_columns)
        with sqlite3.connect(path) as migrated_connection:
            health_tables = {
                row[0]
                for row in migrated_connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("harness_health", health_tables)


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
