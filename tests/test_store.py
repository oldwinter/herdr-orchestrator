from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
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

    def test_enqueue_rejects_changed_dedupe_contract(self) -> None:
        job = _job("contract")
        job_id, created = self.store.enqueue(job)
        self.assertTrue(created)

        variants = (
            {"title": "changed title"},
            {"prompt": "changed prompt"},
            {"harness": Harness.DROID},
            {"placement": PlacementTarget.PANE},
            {"receipt": TaskReceipt(ReceiptKind.FILE, "receipt.txt")},
        )
        for changes in variants:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(StoreError, "dedupe_contract_conflict"),
            ):
                self.store.enqueue(replace(job, **changes))

        replay_id, replay_created = self.store.enqueue(replace(job, max_attempts=9))
        self.assertEqual((replay_id, replay_created), (job_id, False))

    def test_claims_only_one_job_per_harness(self) -> None:
        self.store.enqueue(_job("one", Harness.CODEX))
        self.store.enqueue(_job("two", Harness.CODEX))
        self.store.enqueue(_job("three", Harness.DROID))

        claimed = self.store.claim("example", limit=3, lease_seconds=60)

        self.assertEqual(len(claimed), 2)
        self.assertEqual({job.harness for job in claimed}, {Harness.CODEX, Harness.DROID})
        self.assertEqual({job.agent_name for job in claimed}, {"ho-codex", "ho-droid"})

    def test_claim_with_non_positive_limit_is_a_noop(self) -> None:
        self.store.enqueue(_job("zero"))

        claimed = self.store.claim("example", limit=0, lease_seconds=60)

        self.assertEqual(claimed, [])
        self.assertEqual(self.store.status_counts("example")["pending"], 1)

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
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
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

    def test_outcome_with_wrong_correlation_is_rejected_without_receipt(self) -> None:
        self.store.enqueue(_job("correlation"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        with self.assertRaisesRegex(StoreError, "job_lease_lost"):
            self.store.record_outcome(
                claimed,
                DispatchOutcome(
                    claimed.agent_name,
                    AgentState.DONE,
                    False,
                    "pane",
                    correlation_id="stale-correlation",
                ),
            )

        self.assertEqual(self.store.jobs("example")[0]["state"], JobState.RUNNING.value)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_outcome_after_lease_expiry_is_rejected(self) -> None:
        self.store.enqueue(_job("expired"))
        baseline = time.time() + 1
        with patch("herdr_orchestrator.store.time.time", return_value=baseline):
            claimed = self.store.claim("example", limit=1, lease_seconds=30)[0]

        with (
            patch("herdr_orchestrator.store.time.time", return_value=baseline + 31),
            self.assertRaisesRegex(StoreError, "job_lease_lost"),
        ):
            self.store.record_outcome(
                claimed,
                DispatchOutcome(claimed.agent_name, AgentState.DONE, False, "pane"),
            )

        self.assertEqual(self.store.jobs("example")[0]["state"], JobState.RUNNING.value)

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
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]

        self.assertEqual(pane_id, "w1:p2")
        self.assertEqual(blocked.attempt, 1)
        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(receipt_count, 2)

    def test_resume_lease_has_a_fence_and_persists_correlation(self) -> None:
        job_id, _ = self.store.enqueue(_job("resume-fence", max_attempts=1))
        claimed = self.store.claim("example", limit=1, lease_seconds=30)[0]
        self.store.record_outcome(
            claimed,
            DispatchOutcome(
                claimed.agent_name,
                AgentState.BLOCKED,
                False,
                "w1:p2",
                "agent_blocked",
            ),
        )

        with patch("herdr_orchestrator.store.time.time", return_value=time.time()):
            first_resume, _ = self.store.claim_blocked_for_resume(
                "example",
                job_id,
                lease_seconds=30,
            )
        self.assertNotEqual(first_resume.correlation_id, "")
        with self.assertRaisesRegex(StoreError, "job_resume_in_progress"):
            self.store.claim_blocked_for_resume("example", job_id, lease_seconds=30)

        resume_time = time.time() + 31
        with patch("herdr_orchestrator.store.time.time", return_value=resume_time):
            second_resume, _ = self.store.claim_blocked_for_resume(
                "example",
                job_id,
                lease_seconds=30,
            )
            self.assertNotEqual(second_resume.correlation_id, first_resume.correlation_id)
            with self.assertRaisesRegex(StoreError, "job_lease_lost"):
                self.store.record_resume_outcome(
                    first_resume,
                    DispatchOutcome(first_resume.agent_name, AgentState.DONE, True, "w1:p2"),
                )
            self.assertEqual(
                self.store.record_resume_outcome(
                    second_resume,
                    DispatchOutcome(second_resume.agent_name, AgentState.DONE, True, "w1:p2"),
                ),
                JobState.SUCCEEDED,
            )

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            correlations = connection.execute(
                "SELECT correlation_id FROM receipts WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        self.assertEqual(
            [row[0] for row in correlations],
            [claimed.correlation_id, second_resume.correlation_id],
        )

    def test_resume_without_owner_fence_is_rejected_after_reclaim(self) -> None:
        job_id, _ = self.store.enqueue(_job("resume-missing-fence", max_attempts=1))
        baseline = time.time() + 1
        with patch("herdr_orchestrator.store.time.time", return_value=baseline):
            claimed = self.store.claim("example", limit=1, lease_seconds=30)[0]
            self.store.record_outcome(
                claimed,
                DispatchOutcome(
                    claimed.agent_name,
                    AgentState.BLOCKED,
                    False,
                    "w1:p2",
                    "agent_blocked",
                ),
            )
            first_resume, _ = self.store.claim_blocked_for_resume(
                "example",
                job_id,
                lease_seconds=30,
            )

        stale_resume = replace(first_resume, correlation_id="")
        with patch("herdr_orchestrator.store.time.time", return_value=baseline + 31):
            second_resume, _ = self.store.claim_blocked_for_resume(
                "example",
                job_id,
                lease_seconds=30,
            )
            with self.assertRaisesRegex(StoreError, "job_lease_lost"):
                self.store.record_resume_outcome(
                    stale_resume,
                    DispatchOutcome(stale_resume.agent_name, AgentState.DONE, True, "w1:p2"),
                )

            self.assertEqual(
                self.store.record_resume_outcome(
                    second_resume,
                    DispatchOutcome(second_resume.agent_name, AgentState.DONE, True, "w1:p2"),
                ),
                JobState.SUCCEEDED,
            )

    def test_explicit_unsettled_signal_prevents_success(self) -> None:
        self.store.enqueue(_job("unsettled", max_attempts=1))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        state = self.store.record_outcome(
            claimed,
            DispatchOutcome(
                claimed.agent_name,
                AgentState.DONE,
                False,
                "pane",
                agent_settled=False,
            ),
        )

        self.assertEqual(state, JobState.FAILED)
        self.assertEqual(self.store.jobs("example")[0]["error_code"], "agent_not_settled")

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

    def test_reclaim_clears_previous_attempt_projection(self) -> None:
        self.store.enqueue(_job("clear-projection", max_attempts=3))
        baseline = time.time() + 1
        with patch("herdr_orchestrator.store.time.time", return_value=baseline):
            first = self.store.claim("example", limit=1, lease_seconds=30)[0]
            self.store.record_outcome(
                first,
                DispatchOutcome(
                    first.agent_name,
                    AgentState.UNKNOWN,
                    False,
                    "old-pane",
                    "provider_failed",
                    execution_path="/old/path",
                    herdr_workspace_id="old-workspace",
                    task_verified=False,
                    error_summary="old summary",
                ),
            )

        with patch("herdr_orchestrator.store.time.time", return_value=baseline + 120):
            second = self.store.claim("example", limit=1, lease_seconds=30)[0]

        self.assertEqual(second.attempt, 2)
        current = self.store.jobs("example")[0]
        self.assertEqual(current["state"], JobState.RUNNING.value)
        for field in (
            "error_code",
            "execution_path",
            "herdr_workspace_id",
            "agent_settled",
            "task_verified",
            "error_summary",
        ):
            self.assertIsNone(current[field], field)

    def test_reclaim_records_complete_expired_attempt_receipt(self) -> None:
        self.store.enqueue(_job("lease-receipt", max_attempts=3))
        baseline = time.time() + 1
        with patch("herdr_orchestrator.store.time.time", return_value=baseline):
            first = self.store.claim("example", limit=1, lease_seconds=30)[0]
            with closing(sqlite3.connect(self.store.path)) as connection, connection:
                connection.execute(
                    """
                    UPDATE jobs
                    SET error_code = ?, execution_path = ?, herdr_workspace_id = ?,
                        agent_settled = ?, task_verified = ?, error_summary = ?
                    WHERE id = ?
                    """,
                    (
                        "stale-error",
                        "/old/path",
                        "old-workspace",
                        1,
                        0,
                        "old summary",
                        first.job_id,
                    ),
                )
                connection.commit()

        with patch("herdr_orchestrator.store.time.time", return_value=baseline + 31):
            second = self.store.claim("example", limit=1, lease_seconds=30)[0]

        self.assertEqual(second.attempt, 2)
        current = self.store.jobs("example")[0]
        for field in (
            "error_code",
            "execution_path",
            "herdr_workspace_id",
            "agent_settled",
            "task_verified",
            "error_summary",
        ):
            self.assertIsNone(current[field], field)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            receipt = connection.execute(
                """
                SELECT attempt, state, agent_name, agent_state, member_reused, pane_id,
                       error_code, placement, execution_path, herdr_workspace_id,
                       agent_settled, task_verified, error_summary, correlation_id
                FROM receipts WHERE job_id = ? ORDER BY id
                """,
                (first.job_id,),
            ).fetchone()

        self.assertEqual(
            receipt,
            (
                1,
                JobState.PENDING.value,
                first.agent_name,
                AgentState.UNKNOWN.value,
                0,
                None,
                "lease_expired",
                PlacementTarget.TAB.value,
                "/old/path",
                "old-workspace",
                1,
                0,
                "old summary",
                first.correlation_id,
            ),
        )

    def test_exhausted_lease_records_failed_attempt_receipt(self) -> None:
        self.store.enqueue(_job("lease-exhausted", max_attempts=1))
        baseline = time.time() + 1
        with patch("herdr_orchestrator.store.time.time", return_value=baseline):
            first = self.store.claim("example", limit=1, lease_seconds=30)[0]
        with patch("herdr_orchestrator.store.time.time", return_value=baseline + 31):
            self.assertEqual(self.store.claim("example", limit=1, lease_seconds=30), [])

        self.assertEqual(self.store.jobs("example")[0]["state"], JobState.FAILED.value)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            receipt = connection.execute(
                """
                SELECT attempt, state, agent_name, agent_state, error_code,
                       placement, correlation_id
                FROM receipts WHERE job_id = ?
                """,
                (first.job_id,),
            ).fetchone()
        self.assertEqual(
            receipt,
            (
                1,
                JobState.FAILED.value,
                first.agent_name,
                AgentState.UNKNOWN.value,
                "lease_expired",
                PlacementTarget.TAB.value,
                first.correlation_id,
            ),
        )

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
        with closing(sqlite3.connect(path)) as migrated_connection, migrated_connection:
            version = migrated_connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            job_columns = {row[1] for row in migrated_connection.execute("PRAGMA table_info(jobs)")}
            receipt_columns = {
                row[1] for row in migrated_connection.execute("PRAGMA table_info(receipts)")
            }

        self.assertEqual(version, 4)
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

    def test_migrates_every_supported_schema_version_and_is_repeatable(self) -> None:
        expected_job_columns = {
            "placement",
            "execution_path",
            "herdr_workspace_id",
            "receipt_kind",
            "receipt_value",
            "agent_settled",
            "task_verified",
            "error_summary",
            "correlation_id",
        }
        expected_receipt_columns = expected_job_columns - {"receipt_kind", "receipt_value"}

        for version in range(1, 5):
            with self.subTest(version=version):
                path = Path(self.temporary.name) / f"v{version}.db"
                _create_schema_version(path, version)
                store = Store(path)

                store.initialize()
                store.initialize()

                with closing(sqlite3.connect(path)) as connection, connection:
                    current_version = connection.execute(
                        "SELECT version FROM schema_meta"
                    ).fetchone()[0]
                    job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
                    receipt_columns = {
                        row[1] for row in connection.execute("PRAGMA table_info(receipts)")
                    }
                    receipt = connection.execute(
                        "SELECT pane_id, placement, correlation_id FROM receipts"
                    ).fetchone()

                self.assertEqual(current_version, 4)
                self.assertTrue(expected_job_columns <= job_columns)
                self.assertTrue(expected_receipt_columns <= receipt_columns)
                self.assertEqual(store.jobs("example")[0]["placement"], "tab")
                self.assertEqual(receipt, ("v1:p1", "tab", None))

    def test_interrupted_migration_rolls_back_and_can_resume(self) -> None:
        path = Path(self.temporary.name) / "interrupted.db"
        _create_schema_version(path, 1)
        store = Store(path)

        def interrupt(connection: sqlite3.Connection) -> None:
            store._add_column_if_missing(connection, "jobs", "placement", "TEXT")
            raise RuntimeError("migration_interrupted")

        with (
            patch.object(store, "_migrate_v1_to_v2", side_effect=interrupt),
            self.assertRaisesRegex(RuntimeError, "migration_interrupted"),
        ):
            store.initialize()

        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 1)
            self.assertNotIn(
                "placement",
                {row[1] for row in connection.execute("PRAGMA table_info(jobs)")},
            )

        store.initialize()
        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 4)

    def test_partial_migration_state_is_reconciled_on_restart(self) -> None:
        path = Path(self.temporary.name) / "partial.db"
        _create_schema_version(path, 1)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("ALTER TABLE jobs ADD COLUMN placement TEXT")
            connection.execute("UPDATE jobs SET placement = 'pane'")
            connection.commit()

        store = Store(path)
        store.initialize()

        self.assertEqual(store.jobs("example")[0]["placement"], "pane")
        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 4)


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


def _create_schema_version(path: Path, version: int) -> None:
    if version not in {1, 2, 3, 4}:
        raise ValueError(version)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_meta (version INTEGER NOT NULL);
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
        """)
    connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (version,))
    connection.execute("""
        INSERT INTO jobs(
            workflow, title, harness, prompt, dedupe_key, state,
            attempts, max_attempts, available_at, created_at, updated_at
        ) VALUES ('example', 'legacy', 'codex', 'inspect', 'legacy-v1', 'pending', 0, 2, 1, 1, 1)
        """)
    if version >= 2:
        connection.execute("ALTER TABLE jobs ADD COLUMN placement TEXT")
        connection.execute("UPDATE jobs SET placement = 'tab'")
        connection.execute("ALTER TABLE jobs ADD COLUMN execution_path TEXT")
        connection.execute("ALTER TABLE jobs ADD COLUMN herdr_workspace_id TEXT")
        connection.execute("ALTER TABLE receipts ADD COLUMN placement TEXT")
        connection.execute("UPDATE receipts SET placement = 'tab'")
        connection.execute("ALTER TABLE receipts ADD COLUMN execution_path TEXT")
        connection.execute("ALTER TABLE receipts ADD COLUMN herdr_workspace_id TEXT")
    if version >= 3:
        connection.execute("ALTER TABLE jobs ADD COLUMN receipt_kind TEXT")
        connection.execute("ALTER TABLE jobs ADD COLUMN receipt_value TEXT")
        connection.execute("ALTER TABLE jobs ADD COLUMN agent_settled INTEGER")
        connection.execute("ALTER TABLE jobs ADD COLUMN task_verified INTEGER")
        connection.execute("ALTER TABLE jobs ADD COLUMN error_summary TEXT")
        connection.execute("ALTER TABLE receipts ADD COLUMN agent_settled INTEGER")
        connection.execute("ALTER TABLE receipts ADD COLUMN task_verified INTEGER")
        connection.execute("ALTER TABLE receipts ADD COLUMN error_summary TEXT")
    if version >= 4:
        connection.execute("ALTER TABLE jobs ADD COLUMN correlation_id TEXT")
        connection.execute("ALTER TABLE receipts ADD COLUMN correlation_id TEXT")
    receipt_columns = (
        "job_id, attempt, state, agent_name, agent_state, member_reused, pane_id, error_code"
    )
    receipt_values = "1, 1, 'succeeded', 'worker', 'done', 0, 'v1:p1', NULL"
    if version >= 2:
        receipt_columns += ", placement"
        receipt_values += ", 'tab'"
    receipt_columns += ", observed_at"
    receipt_values += ", 2"
    connection.execute(f"INSERT INTO receipts({receipt_columns}) VALUES ({receipt_values})")
    connection.commit()
    connection.close()


if __name__ == "__main__":
    unittest.main()
