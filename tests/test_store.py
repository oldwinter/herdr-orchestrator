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
    AttemptPhase,
    AttemptProgress,
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

    def test_claim_creates_an_immutable_attempt_owner(self) -> None:
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            job_id, _ = self.store.enqueue(_job("owned-attempt"))
            claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            attempt = connection.execute(
                "SELECT * FROM job_attempts WHERE id = ?",
                (claimed.attempt_id,),
            ).fetchone()

        assert job is not None and attempt is not None
        self.assertEqual(job["current_attempt_id"], claimed.attempt_id)
        self.assertEqual(claimed.attempt, 1)
        self.assertEqual(claimed.phase, AttemptPhase.CLAIMED)
        self.assertTrue(claimed.fencing_token)
        self.assertTrue(claimed.lease_owner)
        self.assertTrue(claimed.operation_token)
        self.assertEqual(claimed.operation_sequence, 0)
        self.assertEqual(claimed.lease_until, 160.0)
        self.assertEqual(attempt["job_id"], job_id)
        self.assertEqual(attempt["attempt"], 1)
        self.assertEqual(attempt["fencing_token"], claimed.fencing_token)
        self.assertEqual(attempt["lease_owner"], claimed.lease_owner)
        self.assertEqual(attempt["lease_until"], 160.0)
        self.assertEqual(attempt["selected_harness"], Harness.CODEX.value)
        self.assertEqual(attempt["agent_name"], claimed.agent_name)
        self.assertEqual(attempt["phase"], AttemptPhase.CLAIMED.value)
        self.assertEqual(attempt["operation_token"], claimed.operation_token)
        status = self.store.jobs("example")[0]
        self.assertEqual(status["current_attempt_id"], claimed.attempt_id)
        self.assertEqual(status["attempt_phase"], AttemptPhase.CLAIMED.value)

    def test_attempt_progress_is_fenced_and_stale_events_are_audited(self) -> None:
        self.store.enqueue(_job("phase-owner"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        acquired = AttemptProgress(
            phase=AttemptPhase.RUNTIME_ACQUIRED,
            agent_name=claimed.agent_name,
            pane_id="w1:p2",
            herdr_workspace_id="w1",
            execution_path="/workspace",
            agent_session_id="session-1",
            prompt_baseline_sequence=10,
        )

        self.store.record_attempt_progress(claimed, acquired)
        stale = replace(claimed, lease_owner="stale-owner")
        with self.assertRaisesRegex(StoreError, "job_lease_lost"):
            self.store.record_attempt_progress(
                stale,
                AttemptProgress(
                    phase=AttemptPhase.PROMPT_ACCEPTED,
                    agent_name=claimed.agent_name,
                    pane_id="w1:p2",
                    herdr_workspace_id="w1",
                    execution_path="/workspace",
                    prompt_accepted_sequence=11,
                    agent_state=AgentState.WORKING,
                ),
            )

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            attempt = connection.execute(
                "SELECT * FROM job_attempts WHERE id = ?",
                (claimed.attempt_id,),
            ).fetchone()
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (claimed.job_id,),
            ).fetchone()
            stale_event = connection.execute(
                "SELECT * FROM receipts WHERE attempt_id = ? AND is_stale = 1",
                (claimed.attempt_id,),
            ).fetchone()

        assert attempt is not None and job is not None and stale_event is not None
        self.assertEqual(attempt["phase"], AttemptPhase.RUNTIME_ACQUIRED.value)
        self.assertEqual(attempt["pane_id"], "w1:p2")
        self.assertEqual(attempt["herdr_workspace_id"], "w1")
        self.assertEqual(attempt["execution_path"], "/workspace")
        self.assertEqual(attempt["agent_session_id"], "session-1")
        self.assertEqual(attempt["prompt_baseline_sequence"], 10)
        self.assertEqual(job["state"], JobState.RUNNING.value)
        self.assertEqual(job["current_attempt_id"], claimed.attempt_id)
        self.assertEqual(stale_event["fencing_token"], claimed.fencing_token)
        self.assertEqual(stale_event["operation_token"], claimed.operation_token)
        self.assertEqual(stale_event["event_kind"], "stale:prompt_accepted")
        self.assertEqual(stale_event["is_stale"], 1)

    def test_outcome_commits_attempt_and_receipt_identity(self) -> None:
        self.store.enqueue(_job("owned-outcome"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        state = self.store.record_outcome(
            claimed,
            DispatchOutcome(
                claimed.agent_name,
                AgentState.DONE,
                False,
                "w1:p2",
                placement=claimed.placement,
                execution_path="/workspace",
                herdr_workspace_id="w1",
                agent_settled=True,
                correlation_id=claimed.correlation_id,
            ),
        )

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            attempt = connection.execute(
                "SELECT * FROM job_attempts WHERE id = ?",
                (claimed.attempt_id,),
            ).fetchone()
            receipt = connection.execute(
                "SELECT * FROM receipts WHERE attempt_id = ? AND is_stale = 0",
                (claimed.attempt_id,),
            ).fetchone()

        assert attempt is not None and receipt is not None
        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertEqual(attempt["phase"], AttemptPhase.OUTCOME_COMMITTED.value)
        self.assertIsNone(attempt["lease_until"])
        self.assertIsNotNone(attempt["finished_at"])
        self.assertEqual(attempt["agent_state"], AgentState.DONE.value)
        self.assertEqual(attempt["agent_settled"], 1)
        self.assertEqual(receipt["attempt_id"], claimed.attempt_id)
        self.assertEqual(receipt["fencing_token"], claimed.fencing_token)
        self.assertEqual(receipt["operation_token"], claimed.operation_token)
        self.assertEqual(receipt["operation_sequence"], 0)
        self.assertEqual(receipt["event_kind"], AttemptPhase.OUTCOME_COMMITTED.value)

    def test_outcome_without_complete_attempt_identity_fails_closed(self) -> None:
        self.store.enqueue(_job("missing-attempt-identity"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        forged = replace(
            claimed,
            attempt_id=0,
            fencing_token="",
            lease_owner="",
            operation_token="",
        )

        with self.assertRaisesRegex(StoreError, "job_lease_lost"):
            self.store.record_outcome(
                forged,
                DispatchOutcome(
                    claimed.agent_name,
                    AgentState.DONE,
                    False,
                    "w1:p2",
                    correlation_id=claimed.correlation_id,
                ),
            )

        job = self.store.jobs("example")[0]
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            phase, lease_until = connection.execute(
                "SELECT phase, lease_until FROM job_attempts WHERE id = ?",
                (claimed.attempt_id,),
            ).fetchone()
        self.assertEqual(job["state"], JobState.RUNNING.value)
        self.assertEqual(phase, AttemptPhase.CLAIMED.value)
        self.assertIsNotNone(lease_until)

    def test_expired_attempt_is_reacquired_before_retry_budget_is_consumed(self) -> None:
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            self.store.enqueue(_job("recover-accepted"))
            first = self.store.claim("example", limit=1, lease_seconds=60)[0]
            self.store.record_attempt_progress(
                first,
                AttemptProgress(
                    phase=AttemptPhase.RUNTIME_ACQUIRED,
                    agent_name=first.agent_name,
                    pane_id="w1:p2",
                    herdr_workspace_id="w1",
                    execution_path="/workspace",
                    agent_session_id="session-1",
                    prompt_baseline_sequence=10,
                ),
            )
            self.store.record_attempt_progress(
                first,
                AttemptProgress(
                    phase=AttemptPhase.PROMPT_ACCEPTED,
                    agent_name=first.agent_name,
                    pane_id="w1:p2",
                    herdr_workspace_id="w1",
                    execution_path="/workspace",
                    agent_session_id="session-1",
                    prompt_accepted_sequence=11,
                    state_change_sequence=11,
                    agent_state=AgentState.WORKING,
                ),
            )

        with patch("herdr_orchestrator.store.time.time", return_value=161.0):
            recovered = self.store.claim("example", limit=1, lease_seconds=60)[0]

        self.assertTrue(recovered.recovery)
        self.assertEqual(recovered.attempt_id, first.attempt_id)
        self.assertEqual(recovered.attempt, first.attempt)
        self.assertEqual(recovered.fencing_token, first.fencing_token)
        self.assertEqual(recovered.operation_token, first.operation_token)
        self.assertNotEqual(recovered.lease_owner, first.lease_owner)
        self.assertEqual(recovered.lease_until, 221.0)
        self.assertEqual(recovered.phase, AttemptPhase.PROMPT_ACCEPTED)
        self.assertIsNotNone(recovered.runtime)
        assert recovered.runtime is not None
        self.assertEqual(recovered.runtime.pane_id, "w1:p2")
        self.assertEqual(recovered.runtime.herdr_workspace_id, "w1")
        self.assertEqual(recovered.runtime.execution_path, "/workspace")
        self.assertEqual(recovered.runtime.agent_session_id, "session-1")
        self.assertEqual(recovered.runtime.prompt_baseline_sequence, 10)
        self.assertEqual(recovered.runtime.prompt_accepted_sequence, 11)
        self.assertEqual(recovered.runtime.state_change_sequence, 11)
        self.assertEqual(self.store.jobs("example")[0]["attempts"], 1)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            attempt_count = connection.execute("SELECT COUNT(*) FROM job_attempts").fetchone()[0]
            self.assertEqual(attempt_count, 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_unaccepted_expired_attempt_is_abandoned_before_replacement(self) -> None:
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            self.store.enqueue(_job("recover-unaccepted"))
            first = self.store.claim("example", limit=1, lease_seconds=60)[0]
        with patch("herdr_orchestrator.store.time.time", return_value=161.0):
            recovered = self.store.claim("example", limit=1, lease_seconds=60)[0]
            state = self.store.record_outcome(
                recovered,
                DispatchOutcome(
                    recovered.agent_name,
                    AgentState.UNKNOWN,
                    False,
                    None,
                    error_code="lease_expired_unaccepted",
                    correlation_id=recovered.correlation_id,
                ),
            )

        self.assertEqual(state, JobState.PENDING)
        self.assertEqual(recovered.attempt_id, first.attempt_id)
        self.assertEqual(self.store.jobs("example")[0]["attempts"], 1)
        with patch("herdr_orchestrator.store.time.time", return_value=163.0):
            replacement = self.store.claim("example", limit=1, lease_seconds=60)[0]

        self.assertFalse(replacement.recovery)
        self.assertEqual(replacement.attempt, 2)
        self.assertNotEqual(replacement.attempt_id, first.attempt_id)
        self.assertNotEqual(replacement.fencing_token, first.fencing_token)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            abandoned = connection.execute(
                """
                SELECT phase, error_code, finished_at
                FROM job_attempts WHERE id = ?
                """,
                (first.attempt_id,),
            ).fetchone()
            receipt = connection.execute(
                """
                SELECT event_kind, attempt_id, fencing_token
                FROM receipts WHERE attempt_id = ? AND is_stale = 0
                """,
                (first.attempt_id,),
            ).fetchone()

        self.assertEqual(
            abandoned,
            (AttemptPhase.ABANDONED.value, "lease_expired_unaccepted", 161.0),
        )
        self.assertEqual(
            receipt,
            (AttemptPhase.ABANDONED.value, first.attempt_id, first.fencing_token),
        )

    def test_unsafe_accepted_recovery_enters_non_resumable_attention(self) -> None:
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            job_id, _ = self.store.enqueue(_job("unsafe-recovery"))
            first = self.store.claim("example", limit=1, lease_seconds=60)[0]
            self.store.record_attempt_progress(
                first,
                AttemptProgress(
                    AttemptPhase.RUNTIME_ACQUIRED,
                    first.agent_name,
                    pane_id="w1:p2",
                    herdr_workspace_id="w1",
                    execution_path="/workspace",
                    prompt_baseline_sequence=10,
                ),
            )
            self.store.record_attempt_progress(
                first,
                AttemptProgress(
                    AttemptPhase.PROMPT_ACCEPTED,
                    first.agent_name,
                    pane_id="w1:p2",
                    herdr_workspace_id="w1",
                    execution_path="/workspace",
                    prompt_accepted_sequence=11,
                    state_change_sequence=11,
                ),
            )
        with patch("herdr_orchestrator.store.time.time", return_value=161.0):
            recovered = self.store.claim("example", limit=1, lease_seconds=60)[0]
            state = self.store.record_outcome(
                recovered,
                DispatchOutcome(
                    recovered.agent_name,
                    AgentState.UNKNOWN,
                    True,
                    "w1:p2",
                    error_code="unsafe_turn_adoption",
                    execution_path="/workspace",
                    herdr_workspace_id="w1",
                    correlation_id=recovered.correlation_id,
                ),
            )

        self.assertEqual(state, JobState.BLOCKED)
        job = self.store.jobs("example")[0]
        self.assertEqual(job["state"], JobState.BLOCKED.value)
        self.assertEqual(job["error_code"], "unsafe_turn_adoption")
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            phase = connection.execute(
                "SELECT phase FROM job_attempts WHERE id = ?",
                (recovered.attempt_id,),
            ).fetchone()[0]
            event = connection.execute(
                "SELECT event_kind FROM receipts WHERE attempt_id = ?",
                (recovered.attempt_id,),
            ).fetchone()[0]
        self.assertEqual(phase, AttemptPhase.ATTENTION.value)
        self.assertEqual(event, AttemptPhase.ATTENTION.value)
        with self.assertRaisesRegex(StoreError, "job_not_resumable"):
            self.store.claim_blocked_for_resume("example", job_id, lease_seconds=60)
        with patch("herdr_orchestrator.store.time.time", return_value=500.0):
            self.assertEqual(self.store.claim("example", limit=1, lease_seconds=60), [])

    def test_accepted_turn_timeout_enters_attention_instead_of_retrying(self) -> None:
        self.store.enqueue(_job("accepted-timeout"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        self.store.record_attempt_progress(
            claimed,
            AttemptProgress(
                AttemptPhase.RUNTIME_ACQUIRED,
                claimed.agent_name,
                pane_id="w1:p2",
                herdr_workspace_id="w1",
                execution_path="/workspace",
                prompt_baseline_sequence=10,
            ),
        )
        self.store.record_attempt_progress(
            claimed,
            AttemptProgress(
                AttemptPhase.PROMPT_ACCEPTED,
                claimed.agent_name,
                pane_id="w1:p2",
                prompt_accepted_sequence=11,
                state_change_sequence=11,
                agent_state=AgentState.WORKING,
            ),
        )

        state = self.store.record_outcome(
            claimed,
            DispatchOutcome(
                claimed.agent_name,
                AgentState.UNKNOWN,
                False,
                "w1:p2",
                "herdr_timeout",
                correlation_id=claimed.correlation_id,
            ),
        )

        self.assertEqual(state, JobState.BLOCKED)
        job = self.store.jobs("example")[0]
        self.assertEqual(job["state"], JobState.BLOCKED.value)
        self.assertEqual(job["error_code"], "herdr_timeout")
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            phase = connection.execute(
                "SELECT phase FROM job_attempts WHERE id = ?",
                (claimed.attempt_id,),
            ).fetchone()[0]
            event = connection.execute(
                "SELECT event_kind FROM receipts WHERE attempt_id = ?",
                (claimed.attempt_id,),
            ).fetchone()[0]
        self.assertEqual(phase, AttemptPhase.ATTENTION.value)
        self.assertEqual(event, AttemptPhase.ATTENTION.value)
        self.assertEqual(self.store.claim("example", limit=1, lease_seconds=60), [])

    def test_recovered_receipt_without_durable_baseline_enters_attention(self) -> None:
        self.store.enqueue(_job("receipt-recovery-attention"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        self.store.record_attempt_progress(
            claimed,
            AttemptProgress(
                AttemptPhase.RUNTIME_ACQUIRED,
                claimed.agent_name,
                pane_id="w1:p2",
                prompt_baseline_sequence=10,
            ),
        )
        self.store.record_attempt_progress(
            claimed,
            AttemptProgress(
                AttemptPhase.PROMPT_ACCEPTED,
                claimed.agent_name,
                pane_id="w1:p2",
                prompt_accepted_sequence=11,
            ),
        )

        state = self.store.record_outcome(
            claimed,
            DispatchOutcome(
                claimed.agent_name,
                AgentState.DONE,
                True,
                "w1:p2",
                "task_receipt_recovery_unverified",
                correlation_id=claimed.correlation_id,
            ),
        )

        self.assertEqual(state, JobState.BLOCKED)
        job = self.store.jobs("example")[0]
        self.assertEqual(job["attempt_phase"], AttemptPhase.ATTENTION.value)
        self.assertEqual(job["error_code"], "task_receipt_recovery_unverified")

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

    def test_outcome_with_wrong_correlation_is_rejected_and_audited(self) -> None:
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
            receipt = connection.execute("""
                SELECT correlation_id, fencing_token, event_kind, is_stale
                FROM receipts
                """).fetchone()
        self.assertEqual(
            receipt,
            (
                "stale-correlation",
                claimed.fencing_token,
                "stale:outcome",
                1,
            ),
        )

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

    def test_resume_rotates_operation_identity_without_changing_attempt(self) -> None:
        self.store.enqueue(_job("resume-operation", max_attempts=1))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        self.store.record_outcome(
            claimed,
            DispatchOutcome(
                claimed.agent_name,
                AgentState.BLOCKED,
                False,
                "w1:p2",
                "agent_blocked",
                execution_path="/workspace",
                herdr_workspace_id="w1",
                correlation_id=claimed.correlation_id,
            ),
        )

        resumed, pane_id = self.store.claim_blocked_for_resume(
            "example",
            claimed.job_id,
            lease_seconds=60,
        )

        self.assertEqual(pane_id, "w1:p2")
        self.assertEqual(resumed.attempt_id, claimed.attempt_id)
        self.assertEqual(resumed.attempt, claimed.attempt)
        self.assertEqual(resumed.fencing_token, claimed.fencing_token)
        self.assertNotEqual(resumed.lease_owner, claimed.lease_owner)
        self.assertNotEqual(resumed.operation_token, claimed.operation_token)
        self.assertTrue(resumed.operation_token)
        self.assertEqual(resumed.operation_sequence, 1)
        self.assertFalse(resumed.recovery)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            operation = connection.execute(
                """
                SELECT operation_token, operation_sequence, operation_kind,
                       lease_owner, lease_until
                FROM job_attempts WHERE id = ?
                """,
                (claimed.attempt_id,),
            ).fetchone()
        self.assertEqual(operation[0], resumed.operation_token)
        self.assertEqual(operation[1], 1)
        self.assertEqual(operation[2], "resume")
        self.assertEqual(operation[3], resumed.lease_owner)
        self.assertEqual(operation[4], resumed.lease_until)

    def test_fresh_resume_clears_prior_operation_evidence(self) -> None:
        self.store.enqueue(_job("resume-evidence", max_attempts=1))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        self.store.record_attempt_progress(
            claimed,
            AttemptProgress(
                AttemptPhase.RUNTIME_ACQUIRED,
                claimed.agent_name,
                pane_id="w1:p2",
                herdr_workspace_id="w1",
                execution_path="/workspace",
                agent_session_id="dispatch-session",
                prompt_baseline_sequence=10,
            ),
        )
        self.store.record_attempt_progress(
            claimed,
            AttemptProgress(
                AttemptPhase.PROMPT_ACCEPTED,
                claimed.agent_name,
                pane_id="w1:p2",
                prompt_accepted_sequence=11,
                state_change_sequence=11,
            ),
        )
        self.store.record_outcome(
            claimed,
            DispatchOutcome(
                claimed.agent_name,
                AgentState.BLOCKED,
                False,
                "w1:p2",
                "agent_blocked",
                correlation_id=claimed.correlation_id,
            ),
        )

        resumed, _ = self.store.claim_blocked_for_resume(
            "example",
            claimed.job_id,
            lease_seconds=60,
        )

        self.assertFalse(resumed.recovery)
        self.assertEqual(resumed.phase, AttemptPhase.CLAIMED)
        assert resumed.runtime is not None
        self.assertIsNone(resumed.runtime.agent_session_id)
        self.assertIsNone(resumed.runtime.prompt_baseline_sequence)
        self.assertIsNone(resumed.runtime.prompt_accepted_sequence)
        self.assertIsNone(resumed.runtime.state_change_sequence)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            evidence = connection.execute(
                """
                SELECT agent_session_id, prompt_baseline_sequence,
                       prompt_accepted_sequence, last_state_change_sequence,
                       agent_state, member_reused, agent_settled, task_verified
                FROM job_attempts WHERE id = ?
                """,
                (claimed.attempt_id,),
            ).fetchone()
        self.assertEqual(evidence, (None, None, None, None, None, None, None, None))

    def test_stale_audit_event_does_not_replace_authoritative_resume_pane(self) -> None:
        self.store.enqueue(_job("resume-after-stale", max_attempts=1))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]
        self.store.record_outcome(
            claimed,
            DispatchOutcome(
                claimed.agent_name,
                AgentState.BLOCKED,
                False,
                "w1:p2",
                "agent_blocked",
                correlation_id=claimed.correlation_id,
            ),
        )
        with self.assertRaisesRegex(StoreError, "job_lease_lost"):
            self.store.record_attempt_progress(
                replace(claimed, lease_owner="stale-owner"),
                AttemptProgress(
                    AttemptPhase.PROMPT_ACCEPTED,
                    claimed.agent_name,
                    pane_id=None,
                    prompt_accepted_sequence=99,
                ),
            )

        resumed, pane_id = self.store.claim_blocked_for_resume(
            "example",
            claimed.job_id,
            lease_seconds=60,
        )

        self.assertEqual(pane_id, "w1:p2")
        self.assertEqual(resumed.attempt_id, claimed.attempt_id)
        self.assertEqual(resumed.fencing_token, claimed.fencing_token)

    def test_expired_accepted_resume_reacquires_the_same_operation(self) -> None:
        with patch("herdr_orchestrator.store.time.time", return_value=50.0):
            self.store.enqueue(_job("resume-recovery", max_attempts=1))
            claimed = self.store.claim("example", limit=1, lease_seconds=30)[0]
            self.store.record_outcome(
                claimed,
                DispatchOutcome(
                    claimed.agent_name,
                    AgentState.BLOCKED,
                    False,
                    "w1:p2",
                    "agent_blocked",
                    correlation_id=claimed.correlation_id,
                ),
            )
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            first_resume, _ = self.store.claim_blocked_for_resume(
                "example",
                claimed.job_id,
                lease_seconds=60,
            )
            self.store.record_attempt_progress(
                first_resume,
                AttemptProgress(
                    AttemptPhase.RUNTIME_ACQUIRED,
                    first_resume.agent_name,
                    pane_id="w1:p2",
                    herdr_workspace_id="w1",
                    execution_path="/workspace",
                    prompt_baseline_sequence=20,
                ),
            )
            self.store.record_attempt_progress(
                first_resume,
                AttemptProgress(
                    AttemptPhase.PROMPT_ACCEPTED,
                    first_resume.agent_name,
                    pane_id="w1:p2",
                    herdr_workspace_id="w1",
                    execution_path="/workspace",
                    prompt_accepted_sequence=21,
                    state_change_sequence=21,
                ),
            )

        with patch("herdr_orchestrator.store.time.time", return_value=161.0):
            recovered, _ = self.store.claim_blocked_for_resume(
                "example",
                claimed.job_id,
                lease_seconds=60,
            )

        self.assertTrue(recovered.recovery)
        self.assertEqual(recovered.attempt_id, first_resume.attempt_id)
        self.assertEqual(recovered.fencing_token, first_resume.fencing_token)
        self.assertEqual(recovered.operation_token, first_resume.operation_token)
        self.assertEqual(recovered.operation_sequence, first_resume.operation_sequence)
        self.assertNotEqual(recovered.lease_owner, first_resume.lease_owner)
        self.assertEqual(recovered.phase, AttemptPhase.PROMPT_ACCEPTED)
        assert recovered.runtime is not None
        self.assertEqual(recovered.runtime.prompt_accepted_sequence, 21)

    def test_expired_unstarted_resume_is_preserved_before_token_rotation(self) -> None:
        with patch("herdr_orchestrator.store.time.time", return_value=50.0):
            self.store.enqueue(_job("resume-claim-history", max_attempts=1))
            claimed = self.store.claim("example", limit=1, lease_seconds=30)[0]
            self.store.record_outcome(
                claimed,
                DispatchOutcome(
                    claimed.agent_name,
                    AgentState.BLOCKED,
                    False,
                    "w1:p2",
                    "agent_blocked",
                    correlation_id=claimed.correlation_id,
                ),
            )
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            first_resume, _ = self.store.claim_blocked_for_resume(
                "example", claimed.job_id, lease_seconds=60
            )
        with patch("herdr_orchestrator.store.time.time", return_value=161.0):
            second_resume, _ = self.store.claim_blocked_for_resume(
                "example", claimed.job_id, lease_seconds=60
            )
            self.store.record_resume_outcome(
                second_resume,
                DispatchOutcome(
                    second_resume.agent_name,
                    AgentState.UNKNOWN,
                    True,
                    "w1:p2",
                    "lease_expired_unaccepted",
                    correlation_id=second_resume.correlation_id,
                ),
            )
        with patch("herdr_orchestrator.store.time.time", return_value=163.0):
            third_resume, _ = self.store.claim_blocked_for_resume(
                "example", claimed.job_id, lease_seconds=60
            )

        self.assertTrue(second_resume.recovery)
        self.assertEqual(second_resume.operation_token, first_resume.operation_token)
        self.assertNotEqual(third_resume.operation_token, first_resume.operation_token)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            history = connection.execute(
                """
                SELECT operation_token, operation_sequence, event_kind, error_code
                FROM receipts WHERE attempt_id = ? ORDER BY id
                """,
                (claimed.attempt_id,),
            ).fetchall()
        self.assertEqual(
            history[-1],
            (
                first_resume.operation_token,
                first_resume.operation_sequence,
                AttemptPhase.ABANDONED.value,
                "lease_expired_unaccepted",
            ),
        )

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
            self.assertTrue(second_resume.recovery)
            self.assertEqual(second_resume.operation_token, first_resume.operation_token)
            self.assertEqual(second_resume.operation_sequence, first_resume.operation_sequence)
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
            receipts = connection.execute(
                """
                SELECT correlation_id, operation_token, operation_sequence,
                       event_kind, is_stale
                FROM receipts WHERE job_id = ? ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        self.assertEqual(
            [row[0] for row in receipts],
            [
                claimed.correlation_id,
                first_resume.correlation_id,
                second_resume.correlation_id,
            ],
        )
        self.assertEqual(receipts[0][1:3], (claimed.operation_token, 0))
        self.assertEqual(
            receipts[1][1:5],
            (first_resume.operation_token, 1, "stale:outcome", 1),
        )
        self.assertEqual(
            receipts[2][1:5],
            (second_resume.operation_token, 1, "outcome_committed", 0),
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
        self.assertEqual(second.attempt, 1)
        self.assertEqual(second.attempt_id, first.attempt_id)
        self.assertEqual(second.fencing_token, first.fencing_token)
        self.assertNotEqual(second.lease_owner, first.lease_owner)
        self.assertTrue(second.recovery)

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

    def test_reclaim_defers_expired_receipt_until_reconciliation(self) -> None:
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

        self.assertEqual(second.attempt, 1)
        self.assertTrue(second.recovery)
        current = self.store.jobs("example")[0]
        self.assertEqual(current["error_code"], "stale-error")
        self.assertEqual(current["execution_path"], "/old/path")
        self.assertEqual(current["herdr_workspace_id"], "old-workspace")
        self.assertIs(current["agent_settled"], True)
        self.assertIs(current["task_verified"], False)
        self.assertEqual(current["error_summary"], "old summary")
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE job_id = ?",
                (first.job_id,),
            ).fetchone()[0]
        self.assertEqual(receipt_count, 0)

    def test_exhausted_lease_records_failed_attempt_receipt(self) -> None:
        self.store.enqueue(_job("lease-exhausted", max_attempts=1))
        baseline = time.time() + 1
        with patch("herdr_orchestrator.store.time.time", return_value=baseline):
            first = self.store.claim("example", limit=1, lease_seconds=30)[0]
        with patch("herdr_orchestrator.store.time.time", return_value=baseline + 31):
            recovered = self.store.claim("example", limit=1, lease_seconds=30)[0]
            state = self.store.record_outcome(
                recovered,
                DispatchOutcome(
                    recovered.agent_name,
                    AgentState.UNKNOWN,
                    True,
                    None,
                    "lease_expired_unaccepted",
                    correlation_id=recovered.correlation_id,
                ),
            )

        self.assertEqual(state, JobState.FAILED)
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
                "lease_expired_unaccepted",
                PlacementTarget.TAB.value,
                recovered.correlation_id,
            ),
        )
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            phase = connection.execute(
                "SELECT phase FROM job_attempts WHERE id = ?",
                (first.attempt_id,),
            ).fetchone()[0]
        self.assertEqual(phase, AttemptPhase.ABANDONED.value)

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
        self.assertIn("current_attempt_id", job_columns)
        self.assertIn("execution_path", receipt_columns)
        self.assertIn("herdr_workspace_id", receipt_columns)
        self.assertIn("agent_settled", receipt_columns)
        self.assertIn("task_verified", receipt_columns)
        self.assertIn("error_summary", receipt_columns)
        self.assertIn("correlation_id", receipt_columns)
        self.assertIn("attempt_id", receipt_columns)
        self.assertIn("fencing_token", receipt_columns)
        self.assertIn("operation_token", receipt_columns)
        self.assertIn("operation_sequence", receipt_columns)
        self.assertIn("event_kind", receipt_columns)
        self.assertIn("is_stale", receipt_columns)

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
            "current_attempt_id",
        }
        expected_receipt_columns = (
            expected_job_columns - {"receipt_kind", "receipt_value", "current_attempt_id"}
        ) | {
            "attempt_id",
            "fencing_token",
            "operation_token",
            "operation_sequence",
            "event_kind",
            "is_stale",
        }

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
                    receipt = connection.execute("""
                        SELECT pane_id, placement, correlation_id, attempt_id,
                               fencing_token, event_kind, is_stale
                        FROM receipts
                        """).fetchone()
                    attempts = connection.execute(
                        "SELECT id, phase FROM job_attempts WHERE job_id = 1"
                    ).fetchall()

                self.assertEqual(current_version, 5)
                self.assertTrue(expected_job_columns <= job_columns)
                self.assertTrue(expected_receipt_columns <= receipt_columns)
                self.assertEqual(store.jobs("example")[0]["placement"], "tab")
                self.assertEqual(receipt[0:3], ("v1:p1", "tab", None))
                self.assertEqual(receipt[3], attempts[0][0])
                self.assertEqual(receipt[4:], ("legacy:1:1", "outcome_committed", 0))
                self.assertEqual(attempts[0][1], "outcome_committed")

    def test_v4_migration_backfills_immutable_attempt_ownership(self) -> None:
        path = Path(self.temporary.name) / "v4-attempt.db"
        _create_schema_version(path, 4)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("""
                UPDATE jobs
                SET state = 'succeeded', attempts = 1, lease_until = NULL,
                    agent_name = 'legacy-worker', execution_path = '/legacy/root',
                    herdr_workspace_id = 'legacy-workspace', agent_settled = 1,
                    task_verified = 1, correlation_id = 'legacy-correlation',
                    updated_at = 3
                WHERE id = 1
                """)
            connection.execute("""
                UPDATE receipts
                SET agent_name = 'legacy-worker', execution_path = '/legacy/root',
                    herdr_workspace_id = 'legacy-workspace', agent_settled = 1,
                    task_verified = 1, correlation_id = 'legacy-correlation',
                    observed_at = 3
                WHERE job_id = 1 AND attempt = 1
                """)

        store = Store(path)
        store.initialize()
        store.initialize()

        with closing(sqlite3.connect(path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            job = connection.execute("SELECT * FROM jobs WHERE id = 1").fetchone()
            attempt = connection.execute(
                "SELECT * FROM job_attempts WHERE job_id = 1 AND attempt = 1"
            ).fetchone()
            receipt = connection.execute(
                "SELECT * FROM receipts WHERE job_id = 1 AND attempt = 1"
            ).fetchone()

        self.assertEqual(version, 5)
        self.assertIsNotNone(attempt)
        assert job is not None and attempt is not None and receipt is not None
        self.assertEqual(job["current_attempt_id"], attempt["id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(job["agent_name"], "legacy-worker")
        self.assertEqual(job["correlation_id"], "legacy-correlation")
        self.assertEqual(job["task_verified"], 1)
        self.assertEqual(attempt["fencing_token"], "legacy-correlation")
        self.assertEqual(attempt["selected_harness"], "codex")
        self.assertEqual(attempt["agent_name"], "legacy-worker")
        self.assertEqual(attempt["execution_path"], "/legacy/root")
        self.assertEqual(attempt["herdr_workspace_id"], "legacy-workspace")
        self.assertEqual(attempt["phase"], "outcome_committed")
        self.assertEqual(attempt["task_verified"], 1)
        self.assertEqual(receipt["attempt_id"], attempt["id"])
        self.assertEqual(receipt["fencing_token"], "legacy-correlation")
        self.assertEqual(receipt["event_kind"], "outcome_committed")
        self.assertEqual(receipt["is_stale"], 0)

    def test_v4_migration_uses_unique_fences_for_null_historical_correlations(self) -> None:
        path = Path(self.temporary.name) / "v4-multiple-attempts.db"
        _create_schema_version(path, 4)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("""
                UPDATE jobs
                SET state = 'running', attempts = 2, lease_until = 50,
                    agent_name = 'current-worker', correlation_id = 'current-correlation',
                    updated_at = 20
                WHERE id = 1
                """)
            connection.execute("""
                UPDATE receipts
                SET attempt = 1, correlation_id = NULL, observed_at = 10
                WHERE job_id = 1
                """)

        Store(path).initialize()

        with closing(sqlite3.connect(path)) as connection, connection:
            attempts = connection.execute("""
                SELECT id, attempt, fencing_token
                FROM job_attempts ORDER BY attempt
                """).fetchall()
            current_attempt_id = connection.execute(
                "SELECT current_attempt_id FROM jobs WHERE id = 1"
            ).fetchone()[0]
            receipt = connection.execute(
                "SELECT attempt_id, fencing_token FROM receipts WHERE job_id = 1"
            ).fetchone()

        self.assertEqual(
            [(row[1], row[2]) for row in attempts],
            [(1, "legacy:1:1"), (2, "current-correlation")],
        )
        self.assertEqual(current_attempt_id, attempts[1][0])
        self.assertEqual(receipt, (attempts[0][0], "legacy:1:1"))

    def test_v4_migration_preserves_inflight_blocked_resume_for_recovery(self) -> None:
        path = Path(self.temporary.name) / "v4-blocked-resume.db"
        _create_schema_version(path, 4)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("""
                UPDATE jobs
                SET state = 'blocked', attempts = 1, lease_until = 160,
                    agent_name = 'legacy-worker', correlation_id = 'resume-correlation',
                    execution_path = '/workspace', herdr_workspace_id = 'w1', updated_at = 100
                WHERE id = 1
                """)
            connection.execute("""
                UPDATE receipts
                SET state = 'blocked', agent_name = 'legacy-worker', agent_state = 'blocked',
                    pane_id = 'w1:p2', placement = 'tab', execution_path = '/workspace',
                    herdr_workspace_id = 'w1', correlation_id = 'dispatch-correlation',
                    observed_at = 90
                WHERE job_id = 1
                """)

        migrated = Store(path)
        migrated.initialize()
        with closing(sqlite3.connect(path)) as connection, connection:
            attempt = connection.execute("""
                SELECT id, fencing_token, operation_token, operation_sequence,
                       operation_kind, phase, lease_until, pane_id, agent_state,
                       member_reused, agent_settled, task_verified, error_code, error_summary
                FROM job_attempts WHERE job_id = 1
                """).fetchone()

        self.assertEqual(
            attempt[1:],
            (
                "dispatch-correlation",
                "resume-correlation",
                1,
                "resume",
                AttemptPhase.RUNTIME_ACQUIRED.value,
                160.0,
                "w1:p2",
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )
        restarted = Store(path)
        with patch("herdr_orchestrator.store.time.time", return_value=161.0):
            recovered, pane_id = restarted.claim_blocked_for_resume("example", 1, lease_seconds=60)
        self.assertTrue(recovered.recovery)
        self.assertEqual(recovered.operation_token, "resume-correlation")
        self.assertEqual(recovered.fencing_token, "dispatch-correlation")
        self.assertEqual(recovered.operation_sequence, 1)
        self.assertEqual(pane_id, "w1:p2")

    def test_v1_v3_migration_separates_inflight_resume_from_dispatch_identity(self) -> None:
        for version in range(1, 4):
            with self.subTest(version=version):
                path = Path(self.temporary.name) / f"v{version}-blocked-resume.db"
                _create_schema_version(path, version)
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute("""
                        UPDATE jobs
                        SET state = 'blocked', attempts = 1, lease_until = 160,
                            agent_name = 'legacy-worker', updated_at = 100
                        WHERE id = 1
                        """)
                    connection.execute("""
                        UPDATE receipts
                        SET state = 'blocked', agent_name = 'legacy-worker',
                            agent_state = 'blocked', pane_id = 'w1:p2', observed_at = 90
                        WHERE job_id = 1
                        """)
                    if version >= 2:
                        connection.execute("""
                            UPDATE jobs
                            SET execution_path = '/workspace', herdr_workspace_id = 'w1'
                            WHERE id = 1
                            """)
                        connection.execute("""
                            UPDATE receipts
                            SET execution_path = '/workspace', herdr_workspace_id = 'w1'
                            WHERE job_id = 1
                            """)

                migrated = Store(path)
                migrated.initialize()
                with closing(sqlite3.connect(path)) as connection, connection:
                    attempt = connection.execute("""
                        SELECT fencing_token, operation_token, operation_sequence,
                               operation_kind, phase
                        FROM job_attempts WHERE job_id = 1
                        """).fetchone()
                    receipt = connection.execute("""
                        SELECT fencing_token, operation_token, operation_sequence
                        FROM receipts WHERE job_id = 1
                        """).fetchone()

                assert attempt is not None and receipt is not None
                self.assertEqual(attempt[0], "legacy:1:1")
                self.assertEqual(attempt[1], "legacy-resume:1:1:1")
                self.assertNotEqual(attempt[0], attempt[1])
                self.assertEqual(
                    attempt[2:],
                    (1, "resume", AttemptPhase.RUNTIME_ACQUIRED.value),
                )
                self.assertEqual(receipt, (attempt[0], attempt[0], 0))

                restarted = Store(path)
                with patch("herdr_orchestrator.store.time.time", return_value=161.0):
                    recovered, pane_id = restarted.claim_blocked_for_resume(
                        "example", 1, lease_seconds=60
                    )
                self.assertTrue(recovered.recovery)
                self.assertEqual(recovered.operation_token, attempt[1])
                self.assertEqual(recovered.fencing_token, attempt[0])
                self.assertEqual(recovered.operation_sequence, 1)
                self.assertEqual(pane_id, "w1:p2")

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
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 5)

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
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 5)


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
