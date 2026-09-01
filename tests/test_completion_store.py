from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from herdr_orchestrator.completion import (
    CompletionPolicy,
    CompletionResult,
    CompletionStatus,
    VerificationClass,
)
from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
    PlacementTarget,
)
from herdr_orchestrator.store import Store


class CompletionStoreTests(unittest.TestCase):
    def test_persists_structured_result_with_receipt_observed_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "state.db")
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow="example",
                    title="structured completion",
                    harness=Harness.CODEX,
                    prompt="Do the task",
                    dedupe_key="structured-v2",
                    max_attempts=2,
                    placement=PlacementTarget.TAB,
                    completion_policy=CompletionPolicy.STRUCTURED_V2,
                )
            )
            claimed = store.claim("example", limit=1, lease_seconds=60)[0]
            result = CompletionResult(
                CompletionPolicy.STRUCTURED_V2,
                VerificationClass.VERIFIED,
                CompletionStatus.COMPLETED,
                "Focused tests passed",
                None,
            )

            for progress in (
                AttemptProgress(
                    AttemptPhase.RUNTIME_ACQUIRED,
                    claimed.agent_name,
                    agent_state=AgentState.IDLE,
                ),
                AttemptProgress(
                    AttemptPhase.PROMPT_ACCEPTED,
                    claimed.agent_name,
                    agent_state=AgentState.WORKING,
                ),
                AttemptProgress(
                    AttemptPhase.SETTLED,
                    claimed.agent_name,
                    agent_state=AgentState.DONE,
                    agent_settled=True,
                ),
                AttemptProgress(
                    AttemptPhase.RECEIPT_OBSERVED,
                    claimed.agent_name,
                    agent_state=AgentState.DONE,
                    agent_settled=True,
                    completion=result,
                ),
            ):
                store.record_attempt_progress(claimed, progress)

            with closing(sqlite3.connect(store.path)) as connection:
                connection.row_factory = sqlite3.Row
                observed = connection.execute(
                    "SELECT * FROM job_attempts WHERE id = ?",
                    (claimed.attempt_id,),
                ).fetchone()

            assert observed is not None
            self.assertEqual(observed["phase"], AttemptPhase.RECEIPT_OBSERVED.value)
            self.assertEqual(observed["completion_policy"], CompletionPolicy.STRUCTURED_V2.value)
            self.assertEqual(observed["verification_class"], VerificationClass.VERIFIED.value)
            self.assertEqual(observed["completion_status"], CompletionStatus.COMPLETED.value)
            self.assertEqual(observed["completion_evidence_summary"], "Focused tests passed")
            self.assertIsNone(observed["completion_error_code"])
            self.assertEqual(observed["task_verified"], 1)

            state = store.record_outcome(
                claimed,
                DispatchOutcome(
                    claimed.agent_name,
                    AgentState.DONE,
                    False,
                    "w1:p2",
                    placement=claimed.placement,
                    agent_settled=True,
                    completion=result,
                    correlation_id=claimed.correlation_id,
                ),
            )
            status = store.jobs("example")[0]

            self.assertEqual(state, JobState.SUCCEEDED)
            self.assertEqual(status["completion_policy"], CompletionPolicy.STRUCTURED_V2.value)
            self.assertEqual(status["verification_class"], VerificationClass.VERIFIED.value)
            self.assertEqual(status["completion_status"], CompletionStatus.COMPLETED.value)
            self.assertEqual(status["completion_evidence_summary"], "Focused tests passed")
            self.assertIsNone(status["completion_error_code"])
            self.assertIs(status["task_verified"], True)

    def test_structured_status_controls_job_state_without_losing_verification(self) -> None:
        cases = (
            (
                CompletionStatus.BLOCKED,
                2,
                JobState.BLOCKED,
                "completion_reported_blocked",
            ),
            (
                CompletionStatus.FAILED,
                1,
                JobState.FAILED,
                "completion_reported_failed",
            ),
        )
        for completion_status, max_attempts, expected_state, expected_error in cases:
            with self.subTest(completion_status=completion_status.value):
                with tempfile.TemporaryDirectory() as temporary:
                    store = Store(Path(temporary) / "state.db")
                    store.initialize()
                    store.enqueue(
                        NewJob(
                            workflow="example",
                            title=completion_status.value,
                            harness=Harness.CODEX,
                            prompt="Do the task",
                            dedupe_key=completion_status.value,
                            max_attempts=max_attempts,
                            placement=PlacementTarget.TAB,
                            completion_policy=CompletionPolicy.STRUCTURED_V2,
                        )
                    )
                    claimed = store.claim("example", limit=1, lease_seconds=60)[0]
                    result = CompletionResult(
                        CompletionPolicy.STRUCTURED_V2,
                        VerificationClass.VERIFIED,
                        completion_status,
                        "Current attempt evidence",
                        None,
                    )

                    state = store.record_outcome(
                        claimed,
                        DispatchOutcome(
                            claimed.agent_name,
                            AgentState.DONE,
                            False,
                            "w1:p2",
                            placement=claimed.placement,
                            agent_settled=True,
                            completion=result,
                            correlation_id=claimed.correlation_id,
                        ),
                    )
                    status = store.jobs("example")[0]

                self.assertEqual(state, expected_state)
                self.assertEqual(status["error_code"], expected_error)
                self.assertEqual(status["completion_status"], completion_status.value)
                self.assertEqual(status["verification_class"], VerificationClass.VERIFIED.value)
                self.assertIs(status["task_verified"], True)

    def test_structured_recovery_without_durable_evidence_enters_attention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "state.db")
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow="example",
                    title="structured recovery",
                    harness=Harness.CODEX,
                    prompt="Do the task",
                    dedupe_key="structured-recovery",
                    max_attempts=2,
                    placement=PlacementTarget.TAB,
                    completion_policy=CompletionPolicy.STRUCTURED_V2,
                )
            )
            claimed = store.claim("example", limit=1, lease_seconds=60)[0]
            for progress in (
                AttemptProgress(
                    AttemptPhase.RUNTIME_ACQUIRED,
                    claimed.agent_name,
                    agent_state=AgentState.IDLE,
                ),
                AttemptProgress(
                    AttemptPhase.PROMPT_ACCEPTED,
                    claimed.agent_name,
                    agent_state=AgentState.WORKING,
                ),
                AttemptProgress(
                    AttemptPhase.SETTLED,
                    claimed.agent_name,
                    agent_state=AgentState.DONE,
                    agent_settled=True,
                ),
            ):
                store.record_attempt_progress(claimed, progress)
            failed = CompletionResult(
                CompletionPolicy.STRUCTURED_V2,
                VerificationClass.VERIFICATION_FAILED,
                None,
                None,
                "completion_recovery_unverified",
            )

            state = store.record_outcome(
                claimed,
                DispatchOutcome(
                    claimed.agent_name,
                    AgentState.DONE,
                    True,
                    "w1:p2",
                    error_code="completion_recovery_unverified",
                    placement=claimed.placement,
                    agent_settled=True,
                    completion=failed,
                    correlation_id=claimed.correlation_id,
                ),
            )
            status = store.jobs("example")[0]

        self.assertEqual(state, JobState.BLOCKED)
        self.assertEqual(status["attempt_phase"], AttemptPhase.ATTENTION.value)
        self.assertEqual(status["attempts"], 1)
        self.assertEqual(status["error_code"], "completion_recovery_unverified")
        self.assertEqual(
            status["verification_class"],
            VerificationClass.VERIFICATION_FAILED.value,
        )


if __name__ == "__main__":
    unittest.main()
