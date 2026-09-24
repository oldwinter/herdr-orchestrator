from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
)
from herdr_orchestrator.store import Store, StoreError

_PROGRESS_PHASES = (
    AttemptPhase.CLAIMED,
    AttemptPhase.RUNTIME_ACQUIRED,
    AttemptPhase.PROMPT_ACCEPTED,
    AttemptPhase.SETTLED,
    AttemptPhase.RECEIPT_OBSERVED,
)

# The legal forward chain plus idempotent re-recording of the current phase.
# Terminal phases (outcome_committed, abandoned, attention) are written only by
# outcome classification and are never legal progress targets.
_LEGAL_TRANSITIONS = frozenset(
    {
        (AttemptPhase.CLAIMED, AttemptPhase.CLAIMED),
        (AttemptPhase.CLAIMED, AttemptPhase.RUNTIME_ACQUIRED),
        (AttemptPhase.RUNTIME_ACQUIRED, AttemptPhase.RUNTIME_ACQUIRED),
        (AttemptPhase.RUNTIME_ACQUIRED, AttemptPhase.PROMPT_ACCEPTED),
        (AttemptPhase.PROMPT_ACCEPTED, AttemptPhase.PROMPT_ACCEPTED),
        (AttemptPhase.PROMPT_ACCEPTED, AttemptPhase.SETTLED),
        (AttemptPhase.SETTLED, AttemptPhase.SETTLED),
        (AttemptPhase.SETTLED, AttemptPhase.RECEIPT_OBSERVED),
        (AttemptPhase.RECEIPT_OBSERVED, AttemptPhase.RECEIPT_OBSERVED),
        (AttemptPhase.OUTCOME_COMMITTED, AttemptPhase.RUNTIME_ACQUIRED),
    }
)

_ALL_TRANSITIONS = [
    (current, target)
    for current in _PROGRESS_PHASES
    for target in AttemptPhase
]


def _job(dedupe_key: str, *, max_attempts: int = 3, workflow: str = "example") -> NewJob:
    return NewJob(
        workflow=workflow,
        title=dedupe_key,
        harness=Harness.CODEX,
        prompt="Do the task",
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )


def _progress(phase: AttemptPhase, agent_name: str) -> AttemptProgress:
    return AttemptProgress(phase=phase, agent_name=agent_name)


def _outcome(
    agent_name: str,
    state: AgentState,
    *,
    error_code: str | None = None,
    agent_settled: bool | None = None,
    task_verified: bool | None = None,
    correlation_id: str = "",
) -> DispatchOutcome:
    return DispatchOutcome(
        agent_name,
        state,
        False,
        "w1:p2",
        error_code=error_code,
        agent_settled=agent_settled,
        task_verified=task_verified,
        correlation_id=correlation_id,
    )


class AttemptPhaseTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.db")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _claimed(self, dedupe_key: str, *, max_attempts: int = 3):
        # Each subtest gets its own workflow: claim admits at most one running
        # job per harness slot, so a shared workflow would starve later claims.
        self.store.enqueue(
            _job(dedupe_key, max_attempts=max_attempts, workflow=dedupe_key)
        )
        return self.store.claim(dedupe_key, limit=1, lease_seconds=60)[0]

    def _advance(self, claimed, target: AttemptPhase) -> None:
        if target is AttemptPhase.CLAIMED:
            return
        for phase in _PROGRESS_PHASES:
            if phase is AttemptPhase.CLAIMED:
                continue
            self.store.record_attempt_progress(claimed, _progress(phase, claimed.agent_name))
            if phase is target:
                return
        raise AssertionError(f"unreachable phase {target}")

    def test_forward_chain_reaches_receipt_observed(self) -> None:
        claimed = self._claimed("forward-chain")

        self._advance(claimed, AttemptPhase.RECEIPT_OBSERVED)

        self.assertEqual(
            self.store.attempt_phase(claimed.attempt_id),
            AttemptPhase.RECEIPT_OBSERVED,
        )

    def test_every_illegal_transition_is_rejected(self) -> None:
        illegal = [
            (current, target)
            for current, target in _ALL_TRANSITIONS
            if (current, target) not in _LEGAL_TRANSITIONS
        ]
        self.assertTrue(illegal)
        for index, (current, target) in enumerate(illegal):
            with self.subTest(current=current, target=target):
                claimed = self._claimed(f"illegal-{index}")
                self._advance(claimed, current)

                with self.assertRaisesRegex(StoreError, "attempt_phase_invalid"):
                    self.store.record_attempt_progress(
                        claimed, _progress(target, claimed.agent_name)
                    )

                self.assertEqual(self.store.attempt_phase(claimed.attempt_id), current)

    def test_every_legal_transition_is_accepted(self) -> None:
        for index, (current, target) in enumerate(sorted(_LEGAL_TRANSITIONS, key=repr)):
            if current is AttemptPhase.OUTCOME_COMMITTED:
                # Only reachable as a predecessor through outcome commit, not
                # through progress; covered by the illegal-transition matrix.
                continue
            with self.subTest(current=current, target=target):
                claimed = self._claimed(f"legal-{index}")
                self._advance(claimed, current)

                self.store.record_attempt_progress(
                    claimed, _progress(target, claimed.agent_name)
                )

                self.assertEqual(self.store.attempt_phase(claimed.attempt_id), target)

    def test_terminal_phases_cannot_be_forged_as_progress(self) -> None:
        for index, target in enumerate(
            (
                AttemptPhase.OUTCOME_COMMITTED,
                AttemptPhase.ABANDONED,
                AttemptPhase.ATTENTION,
            )
        ):
            with self.subTest(target=target):
                claimed = self._claimed(f"terminal-{index}")
                self._advance(claimed, AttemptPhase.RECEIPT_OBSERVED)

                with self.assertRaisesRegex(StoreError, "attempt_phase_invalid"):
                    self.store.record_attempt_progress(
                        claimed, _progress(target, claimed.agent_name)
                    )

    def test_progress_after_outcome_commit_fails_closed(self) -> None:
        claimed = self._claimed("post-commit")
        self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.DONE,
                agent_settled=True,
                correlation_id=claimed.correlation_id,
            ),
        )

        with self.assertRaisesRegex(StoreError, "job_lease_lost"):
            self.store.record_attempt_progress(
                claimed,
                _progress(AttemptPhase.RUNTIME_ACQUIRED, claimed.agent_name),
            )


class AttemptOutcomeClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.db")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _claimed(self, dedupe_key: str, *, max_attempts: int = 3):
        # Unique workflow per case: claim admits at most one running job per
        # harness slot, so a shared workflow would starve later claims.
        self.store.enqueue(
            _job(dedupe_key, max_attempts=max_attempts, workflow=dedupe_key)
        )
        return self.store.claim(dedupe_key, limit=1, lease_seconds=60)[0]

    def _advance(self, claimed, target: AttemptPhase) -> None:
        if target is AttemptPhase.CLAIMED:
            return
        for phase in _PROGRESS_PHASES:
            if phase is AttemptPhase.CLAIMED:
                continue
            self.store.record_attempt_progress(claimed, _progress(phase, claimed.agent_name))
            if phase is target:
                return
        raise AssertionError(f"unreachable phase {target}")

    def _job_row(self, workflow: str) -> dict[str, object]:
        return self.store.jobs(workflow)[0]

    def test_settled_done_outcome_succeeds(self) -> None:
        claimed = self._claimed("settled-done")

        state = self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.DONE,
                agent_settled=True,
                correlation_id=claimed.correlation_id,
            ),
        )

        job = self._job_row("settled-done")
        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertEqual(job["state"], JobState.SUCCEEDED.value)
        self.assertIsNone(job["error_code"])
        self.assertEqual(
            self.store.attempt_phase(claimed.attempt_id), AttemptPhase.OUTCOME_COMMITTED
        )

    def test_done_without_settlement_is_retried(self) -> None:
        claimed = self._claimed("done-unsettled")

        state = self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.DONE,
                agent_settled=False,
                correlation_id=claimed.correlation_id,
            ),
        )

        job = self._job_row("done-unsettled")
        self.assertEqual(state, JobState.PENDING)
        self.assertEqual(job["state"], JobState.PENDING.value)
        self.assertEqual(job["error_code"], "agent_not_settled")
        self.assertEqual(
            self.store.attempt_phase(claimed.attempt_id), AttemptPhase.ABANDONED
        )

    def test_working_outcome_before_acceptance_retries_with_backoff(self) -> None:
        claimed = self._claimed("working-retry")

        state = self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.WORKING,
                correlation_id=claimed.correlation_id,
            ),
        )

        job = self._job_row("working-retry")
        self.assertEqual(state, JobState.PENDING)
        self.assertEqual(job["error_code"], "agent_not_settled")
        self.assertEqual(
            self.store.attempt_phase(claimed.attempt_id), AttemptPhase.ABANDONED
        )

    def test_working_outcome_after_receipt_observation_needs_attention(self) -> None:
        claimed = self._claimed("working-attention")
        self._advance(claimed, AttemptPhase.RECEIPT_OBSERVED)

        state = self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.WORKING,
                correlation_id=claimed.correlation_id,
            ),
        )

        job = self._job_row("working-attention")
        self.assertEqual(state, JobState.BLOCKED)
        self.assertEqual(job["state"], JobState.BLOCKED.value)
        self.assertEqual(
            self.store.attempt_phase(claimed.attempt_id), AttemptPhase.ATTENTION
        )

    def test_unsafe_turn_adoption_blocks_for_attention(self) -> None:
        claimed = self._claimed("unsafe-adoption")

        state = self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.UNKNOWN,
                error_code="unsafe_turn_adoption",
                agent_settled=False,
                correlation_id=claimed.correlation_id,
            ),
        )

        job = self._job_row("unsafe-adoption")
        self.assertEqual(state, JobState.BLOCKED)
        self.assertEqual(job["state"], JobState.BLOCKED.value)
        self.assertEqual(job["error_code"], "unsafe_turn_adoption")
        self.assertEqual(
            self.store.attempt_phase(claimed.attempt_id), AttemptPhase.ATTENTION
        )

    def test_blocked_agent_outcome_commits_blocked_job(self) -> None:
        claimed = self._claimed("agent-blocked")

        state = self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.BLOCKED,
                correlation_id=claimed.correlation_id,
            ),
        )

        job = self._job_row("agent-blocked")
        self.assertEqual(state, JobState.BLOCKED)
        self.assertEqual(job["state"], JobState.BLOCKED.value)
        self.assertEqual(
            self.store.attempt_phase(claimed.attempt_id), AttemptPhase.OUTCOME_COMMITTED
        )

    def test_unverified_receipt_is_retried(self) -> None:
        claimed = self._claimed("receipt-invalid")

        state = self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.DONE,
                agent_settled=True,
                task_verified=False,
                correlation_id=claimed.correlation_id,
            ),
        )

        job = self._job_row("receipt-invalid")
        self.assertEqual(state, JobState.PENDING)
        self.assertEqual(job["error_code"], "task_receipt_invalid")

    def test_last_attempt_failure_exhausts_to_failed(self) -> None:
        claimed = self._claimed("exhausted", max_attempts=1)

        state = self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.WORKING,
                correlation_id=claimed.correlation_id,
            ),
        )

        job = self._job_row("exhausted")
        self.assertEqual(state, JobState.FAILED)
        self.assertEqual(job["state"], JobState.FAILED.value)
        self.assertEqual(job["error_code"], "agent_not_settled")

    def test_retry_backoff_doubles_and_caps(self) -> None:
        now = 1000.0
        expected_deltas = [1, 2, 4, 8, 16, 32, 60]
        with patch("herdr_orchestrator.store.time.time", return_value=now):
            self.store.enqueue(
                _job("backoff", max_attempts=len(expected_deltas) + 1, workflow="backoff")
            )
        observed: list[float] = []
        for delta in expected_deltas:
            with patch("herdr_orchestrator.store.time.time", return_value=now):
                claimed = self.store.claim("backoff", limit=1, lease_seconds=60)[0]
                self.store.record_outcome(
                    claimed,
                    _outcome(
                        claimed.agent_name,
                        AgentState.WORKING,
                        correlation_id=claimed.correlation_id,
                    ),
                )
            with closing(sqlite3.connect(self.store.path)) as connection:
                available_at = connection.execute(
                    "SELECT available_at FROM jobs WHERE workflow = 'backoff'"
                ).fetchone()[0]
            observed.append(available_at - now)
            now = available_at

        self.assertEqual(observed, expected_deltas)

    def test_resume_outcome_succeeds_only_when_agent_settled(self) -> None:
        claimed = self._claimed("resume-settled")
        self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.BLOCKED,
                correlation_id=claimed.correlation_id,
            ),
        )
        resumed, _ = self.store.claim_blocked_for_resume(
            "resume-settled", claimed.job_id, lease_seconds=60
        )

        state = self.store.record_resume_outcome(
            resumed,
            _outcome(
                resumed.agent_name,
                AgentState.DONE,
                agent_settled=True,
                correlation_id=resumed.correlation_id,
            ),
        )

        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertEqual(self._job_row("resume-settled")["state"], JobState.SUCCEEDED.value)

    def test_resume_outcome_for_unsettled_agent_stays_blocked(self) -> None:
        claimed = self._claimed("resume-unsettled")
        self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.BLOCKED,
                correlation_id=claimed.correlation_id,
            ),
        )
        resumed, _ = self.store.claim_blocked_for_resume(
            "resume-unsettled", claimed.job_id, lease_seconds=60
        )

        state = self.store.record_resume_outcome(
            resumed,
            _outcome(
                resumed.agent_name,
                AgentState.WORKING,
                correlation_id=resumed.correlation_id,
            ),
        )

        job = self._job_row("resume-unsettled")
        self.assertEqual(state, JobState.BLOCKED)
        self.assertEqual(job["state"], JobState.BLOCKED.value)
        self.assertEqual(job["error_code"], "agent_not_settled")

    def test_attention_attempt_resumes_with_fresh_operation(self) -> None:
        claimed = self._claimed("attention-resume")
        self._advance(claimed, AttemptPhase.RECEIPT_OBSERVED)
        self.store.record_outcome(
            claimed,
            _outcome(
                claimed.agent_name,
                AgentState.WORKING,
                correlation_id=claimed.correlation_id,
            ),
        )
        self.assertEqual(
            self.store.attempt_phase(claimed.attempt_id), AttemptPhase.ATTENTION
        )

        resumed, pane_id = self.store.claim_blocked_for_resume(
            "attention-resume", claimed.job_id, lease_seconds=60
        )

        self.assertEqual(pane_id, "w1:p2")
        self.assertFalse(resumed.recovery)
        self.assertEqual(resumed.operation_sequence, 1)
        self.assertEqual(resumed.phase, AttemptPhase.CLAIMED)
        with self.assertRaisesRegex(StoreError, "job_resume_in_progress"):
            self.store.claim_blocked_for_resume(
                "attention-resume", claimed.job_id, lease_seconds=60
            )

        state = self.store.record_resume_outcome(
            resumed,
            _outcome(
                resumed.agent_name,
                AgentState.DONE,
                agent_settled=True,
                correlation_id=resumed.correlation_id,
            ),
        )

        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertEqual(
            self._job_row("attention-resume")["state"], JobState.SUCCEEDED.value
        )
        self.assertEqual(
            self.store.attempt_phase(claimed.attempt_id), AttemptPhase.OUTCOME_COMMITTED
        )


if __name__ == "__main__":
    unittest.main()
