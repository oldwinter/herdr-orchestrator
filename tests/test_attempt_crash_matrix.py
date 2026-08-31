from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from crash_matrix import CrashAfterTransition, run_public_operation_crash_matrix

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    AttemptRuntime,
    DispatchContext,
    DispatchOutcome,
    Harness,
    NewJob,
)
from herdr_orchestrator.runner import Coordinator, OperationInterrupted
from herdr_orchestrator.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


class RuntimeInterruptDispatcher:
    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        del harness, prompt, timeout_seconds
        assert agent_name is not None and context is not None
        assert context.attempt_progress is not None
        context.attempt_progress(
            AttemptProgress(
                AttemptPhase.RUNTIME_ACQUIRED,
                agent_name,
                pane_id="w1:p2",
                herdr_workspace_id="w1",
                execution_path="/workspace",
                prompt_baseline_sequence=10,
            )
        )
        raise AssertionError("the crash must stop dispatch before the prompt")


class PersistentAttemptDispatcher:
    def __init__(self, *, crash_before_acceptance: bool = False) -> None:
        self.state = AgentState.IDLE
        self.sequence = 10
        self.prompt_count = 0
        self.crash_before_acceptance = crash_before_acceptance

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        del harness, prompt, timeout_seconds
        assert agent_name is not None and context is not None
        assert context.attempt_progress is not None
        context.attempt_progress(self._progress(AttemptPhase.RUNTIME_ACQUIRED, agent_name))
        self.prompt_count += 1
        self.state = AgentState.WORKING
        self.sequence = 11
        if self.crash_before_acceptance:
            self.crash_before_acceptance = False
            raise OperationInterrupted("before_acceptance_commit")
        context.attempt_progress(self._progress(AttemptPhase.PROMPT_ACCEPTED, agent_name))
        self.state = AgentState.DONE
        self.sequence = 12
        context.attempt_progress(self._progress(AttemptPhase.SETTLED, agent_name))
        context.attempt_progress(self._progress(AttemptPhase.RECEIPT_OBSERVED, agent_name))
        return self._outcome(agent_name)

    def recover(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str,
        context: DispatchContext,
        runtime: AttemptRuntime,
    ) -> DispatchOutcome:
        del harness, prompt, timeout_seconds
        assert context.attempt_progress is not None
        if runtime.prompt_baseline_sequence == self.sequence and self.state is AgentState.BLOCKED:
            return DispatchOutcome(
                agent_name,
                AgentState.UNKNOWN,
                True,
                runtime.pane_id,
                "lease_expired_unaccepted",
                correlation_id=context.correlation_id,
            )
        assert runtime.prompt_baseline_sequence is not None
        if self.sequence == runtime.prompt_baseline_sequence and self.state is AgentState.IDLE:
            return DispatchOutcome(
                agent_name,
                AgentState.UNKNOWN,
                True,
                runtime.pane_id,
                "lease_expired_unaccepted",
                correlation_id=context.correlation_id,
            )
        if runtime.phase in {AttemptPhase.CLAIMED, AttemptPhase.RUNTIME_ACQUIRED}:
            context.attempt_progress(self._progress(AttemptPhase.PROMPT_ACCEPTED, agent_name))
        self.state = AgentState.DONE
        self.sequence = max(12, self.sequence)
        if runtime.phase in {
            AttemptPhase.CLAIMED,
            AttemptPhase.RUNTIME_ACQUIRED,
            AttemptPhase.PROMPT_ACCEPTED,
        }:
            context.attempt_progress(self._progress(AttemptPhase.SETTLED, agent_name))
        if runtime.phase is not AttemptPhase.RECEIPT_OBSERVED:
            context.attempt_progress(self._progress(AttemptPhase.RECEIPT_OBSERVED, agent_name))
        return self._outcome(agent_name)

    def _progress(self, phase: AttemptPhase, agent_name: str) -> AttemptProgress:
        return AttemptProgress(
            phase,
            agent_name,
            pane_id="w1:p2",
            herdr_workspace_id="w1",
            execution_path="/workspace",
            prompt_baseline_sequence=10 if phase is AttemptPhase.RUNTIME_ACQUIRED else None,
            prompt_accepted_sequence=11 if phase is AttemptPhase.PROMPT_ACCEPTED else None,
            state_change_sequence=self.sequence,
            agent_state=self.state,
            agent_settled=self.state is AgentState.DONE,
        )

    def _outcome(self, agent_name: str) -> DispatchOutcome:
        return DispatchOutcome(
            agent_name,
            AgentState.DONE,
            True,
            "w1:p2",
            execution_path="/workspace",
            herdr_workspace_id="w1",
            agent_settled=True,
        )


class PersistentResumeDispatcher(PersistentAttemptDispatcher):
    def __init__(self, *, crash_before_acceptance: bool = False) -> None:
        super().__init__()
        self.state = AgentState.BLOCKED
        self.sequence = 20
        self.response_count = 0
        self.crash_before_acceptance = crash_before_acceptance

    def respond(
        self,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
        expected_pane_id: str,
        context: DispatchContext | None,
    ) -> DispatchOutcome:
        del harness, response, timeout_seconds
        assert context is not None and context.attempt_progress is not None
        assert expected_pane_id == "w1:p2"
        context.attempt_progress(self._resume_progress(AttemptPhase.RUNTIME_ACQUIRED, name))
        self.response_count += 1
        self.state = AgentState.WORKING
        self.sequence = 21
        if self.crash_before_acceptance:
            self.crash_before_acceptance = False
            raise OperationInterrupted("before_acceptance_commit")
        context.attempt_progress(self._resume_progress(AttemptPhase.PROMPT_ACCEPTED, name))
        self.state = AgentState.DONE
        self.sequence = 22
        context.attempt_progress(self._resume_progress(AttemptPhase.SETTLED, name))
        context.attempt_progress(self._resume_progress(AttemptPhase.RECEIPT_OBSERVED, name))
        return self._outcome(name)

    def recover(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str,
        context: DispatchContext,
        runtime: AttemptRuntime,
    ) -> DispatchOutcome:
        del harness, prompt, timeout_seconds
        assert context.attempt_progress is not None
        if runtime.prompt_baseline_sequence == self.sequence and self.state is AgentState.BLOCKED:
            return DispatchOutcome(
                agent_name,
                AgentState.UNKNOWN,
                True,
                runtime.pane_id,
                "lease_expired_unaccepted",
                correlation_id=context.correlation_id,
            )
        if runtime.phase in {AttemptPhase.CLAIMED, AttemptPhase.RUNTIME_ACQUIRED}:
            context.attempt_progress(
                self._resume_progress(AttemptPhase.PROMPT_ACCEPTED, agent_name)
            )
        self.state = AgentState.DONE
        self.sequence = max(22, self.sequence)
        if runtime.phase in {
            AttemptPhase.CLAIMED,
            AttemptPhase.RUNTIME_ACQUIRED,
            AttemptPhase.PROMPT_ACCEPTED,
        }:
            context.attempt_progress(self._resume_progress(AttemptPhase.SETTLED, agent_name))
        if runtime.phase is not AttemptPhase.RECEIPT_OBSERVED:
            context.attempt_progress(
                self._resume_progress(AttemptPhase.RECEIPT_OBSERVED, agent_name)
            )
        return self._outcome(agent_name)

    def _resume_progress(self, phase: AttemptPhase, name: str) -> AttemptProgress:
        return AttemptProgress(
            phase,
            name,
            pane_id="w1:p2",
            herdr_workspace_id="w1",
            execution_path="/workspace",
            prompt_baseline_sequence=20 if phase is AttemptPhase.RUNTIME_ACQUIRED else None,
            prompt_accepted_sequence=21 if phase is AttemptPhase.PROMPT_ACCEPTED else None,
            state_change_sequence=self.sequence,
            agent_state=self.state,
            agent_settled=self.state is AgentState.DONE,
        )


def test_public_run_operation_can_interrupt_after_a_durable_transition() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        config = replace(
            load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
            state_db=Path(temporary) / "state.db",
        )
        config = replace(config, coordinator=replace(config.coordinator, lease_seconds=60))
        store = Store(config.state_db)
        store.initialize()
        store.enqueue(
            NewJob(
                config.name,
                "Crash matrix",
                Harness.DROID,
                "Do the task once.",
                "crash-matrix",
                3,
            )
        )
        crash = CrashAfterTransition(AttemptPhase.RUNTIME_ACQUIRED)
        coordinator = Coordinator(
            config,
            store=store,
            dispatcher=RuntimeInterruptDispatcher(),
            transition_observer=crash,
        )

        with pytest.raises(OperationInterrupted, match="runtime_acquired"):
            coordinator.run_once()

        with closing(sqlite3.connect(config.state_db)) as connection, connection:
            phase = connection.execute("SELECT phase FROM job_attempts").fetchone()[0]

    assert phase == AttemptPhase.RUNTIME_ACQUIRED.value
    assert crash.observed == [AttemptPhase.CLAIMED, AttemptPhase.RUNTIME_ACQUIRED]


def test_run_once_crash_matrix_converges_with_one_prompt() -> None:
    transitions = (
        AttemptPhase.CLAIMED,
        AttemptPhase.RUNTIME_ACQUIRED,
        AttemptPhase.PROMPT_ACCEPTED,
        AttemptPhase.SETTLED,
        AttemptPhase.RECEIPT_OBSERVED,
        AttemptPhase.OUTCOME_COMMITTED,
    )

    results = run_public_operation_crash_matrix(transitions, _exercise_run_once_crash)

    assert set(results) == set(transitions)
    assert all(result["state"] == "succeeded" for result in results.values()), results
    assert all(result["prompt_count"] == 1 for result in results.values())


def test_resume_crash_matrix_converges_with_one_response() -> None:
    transitions = (
        AttemptPhase.CLAIMED,
        AttemptPhase.RUNTIME_ACQUIRED,
        AttemptPhase.PROMPT_ACCEPTED,
        AttemptPhase.SETTLED,
        AttemptPhase.RECEIPT_OBSERVED,
        AttemptPhase.OUTCOME_COMMITTED,
    )

    results = run_public_operation_crash_matrix(transitions, _exercise_resume_crash)

    assert set(results) == set(transitions)
    assert all(result["state"] == "succeeded" for result in results.values()), results
    assert all(result["response_count"] == 1 for result in results.values())


def test_dispatch_acceptance_callback_window_recovers_without_duplicate_prompt() -> None:
    result = _exercise_acceptance_callback_window(resume=False)
    assert result == {"state": "succeeded", "submission_count": 1}


def test_resume_acceptance_callback_window_recovers_without_duplicate_response() -> None:
    result = _exercise_acceptance_callback_window(resume=True)
    assert result == {"state": "succeeded", "submission_count": 1}


def _exercise_run_once_crash(target: AttemptPhase) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary:
        config = replace(
            load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
            state_db=Path(temporary) / "state.db",
        )
        config = replace(config, coordinator=replace(config.coordinator, lease_seconds=60))
        store = Store(config.state_db)
        store.initialize()
        dispatcher = PersistentAttemptDispatcher()
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr("herdr_orchestrator.store.time.time", lambda: 100.0)
            store.enqueue(
                NewJob(
                    config.name,
                    "Crash matrix",
                    Harness.DROID,
                    "Do the task once.",
                    f"crash-{target.value}",
                    3,
                )
            )
            with pytest.raises(OperationInterrupted, match=target.value):
                Coordinator(
                    config,
                    store=store,
                    dispatcher=dispatcher,
                    transition_observer=CrashAfterTransition(target),
                ).run_once()
        reports: list[dict[str, object]] = []
        for observed_at in (161.0, 163.0, 165.0):
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(
                    "herdr_orchestrator.store.time.time",
                    lambda value=observed_at: value,
                )
                reports.append(Coordinator(config, store=store, dispatcher=dispatcher).run_once())
            job = store.jobs(config.name)[0]
            if job["state"] == "succeeded":
                break
        return {
            "state": store.jobs(config.name)[0]["state"],
            "prompt_count": dispatcher.prompt_count,
            "reports": reports,
            "attempt_phase": store.jobs(config.name)[0]["attempt_phase"],
        }


def _exercise_resume_crash(target: AttemptPhase) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary:
        config = replace(
            load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
            state_db=Path(temporary) / "state.db",
        )
        config = replace(config, coordinator=replace(config.coordinator, lease_seconds=60))
        store = Store(config.state_db)
        store.initialize()
        dispatcher = PersistentResumeDispatcher()
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr("herdr_orchestrator.store.time.time", lambda: 50.0)
            job_id, _ = store.enqueue(
                NewJob(
                    config.name,
                    "Resume crash matrix",
                    Harness.DROID,
                    "Do the task once.",
                    f"resume-crash-{target.value}",
                    2,
                )
            )
            claimed = store.claim(config.name, limit=1, lease_seconds=30)[0]
            store.record_outcome(
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
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr("herdr_orchestrator.store.time.time", lambda: 100.0)
            with pytest.raises(OperationInterrupted, match=target.value):
                Coordinator(
                    config,
                    store=store,
                    dispatcher=dispatcher,
                    transition_observer=CrashAfterTransition(target),
                ).resume_blocked(job_id, "Approved")
        for observed_at in (161.0, 163.0):
            if store.jobs(config.name)[0]["state"] == "succeeded":
                break
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(
                    "herdr_orchestrator.store.time.time",
                    lambda value=observed_at: value,
                )
                Coordinator(config, store=store, dispatcher=dispatcher).resume_blocked(
                    job_id,
                    "Approved",
                )
        return {
            "state": store.jobs(config.name)[0]["state"],
            "response_count": dispatcher.response_count,
        }


def _exercise_acceptance_callback_window(*, resume: bool) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary:
        config = replace(
            load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
            state_db=Path(temporary) / "state.db",
        )
        config = replace(config, coordinator=replace(config.coordinator, lease_seconds=60))
        store = Store(config.state_db)
        store.initialize()
        dispatcher: PersistentAttemptDispatcher
        dispatcher = (
            PersistentResumeDispatcher(crash_before_acceptance=True)
            if resume
            else PersistentAttemptDispatcher(crash_before_acceptance=True)
        )
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr("herdr_orchestrator.store.time.time", lambda: 100.0)
            job_id, _ = store.enqueue(
                NewJob(
                    config.name,
                    "Acceptance callback window",
                    Harness.DROID,
                    "Do the task once.",
                    f"callback-window-{resume}",
                    3,
                )
            )
            if resume:
                claimed = store.claim(config.name, limit=1, lease_seconds=60)[0]
                store.record_outcome(
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
                with pytest.raises(OperationInterrupted, match="before_acceptance_commit"):
                    Coordinator(config, store=store, dispatcher=dispatcher).resume_blocked(
                        job_id,
                        "Approved",
                    )
            else:
                with pytest.raises(OperationInterrupted, match="before_acceptance_commit"):
                    Coordinator(config, store=store, dispatcher=dispatcher).run_once()
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr("herdr_orchestrator.store.time.time", lambda: 161.0)
            if resume:
                Coordinator(config, store=store, dispatcher=dispatcher).resume_blocked(
                    job_id,
                    "Approved",
                )
                count = dispatcher.response_count
            else:
                Coordinator(config, store=store, dispatcher=dispatcher).run_once()
                count = dispatcher.prompt_count
        return {"state": store.jobs(config.name)[0]["state"], "submission_count": count}
