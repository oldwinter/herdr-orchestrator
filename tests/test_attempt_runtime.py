from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from herdr_orchestrator.attempt_runtime import (
    AMBIGUOUS_RESPONSE_SUBMISSION_ERRORS,
    recover_turn,
    submit_blocked_response,
)
from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    AttemptRuntime,
    DispatchContext,
    Harness,
    PlacementTarget,
)
from herdr_orchestrator.protocol import TransportError


class FakeRunner:
    def __init__(self, payloads: list[dict[str, object] | str]) -> None:
        self.responses = [
            (
                _text(payload)
                if isinstance(payload, str)
                else _error(str(payload["_error"])) if "_error" in payload else _result(payload)
            )
            for payload in payloads
        ]
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        self.calls.append(argv)
        if not self.responses:
            raise AssertionError(f"unexpected call: {argv}")
        return self.responses.pop(0)


class FakeHost:
    """Minimal RecoveryHost: scripted runner, no real sleeping."""

    def __init__(
        self,
        workspace: Path,
        runner: FakeRunner,
        *,
        settled_confirmation_polls: int = 0,
        runtime_error: TransportError | None = None,
    ) -> None:
        self.workspace = workspace
        self.runner = runner
        self.sleeps: list[float] = []
        self.settled_confirmation_polls = settled_confirmation_polls
        self._dispatch_deadline = SimpleNamespace(value=float("inf"))
        self._runtime_error = runtime_error
        self.environment_checks = 0

    def sleeper(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def check_environment(self) -> None:
        self.environment_checks += 1

    def _sleep_until(self, seconds: float, deadline: float) -> None:
        del deadline
        self.sleeps.append(seconds)

    def _confirm_stable_settlement(
        self,
        name: str,
        current: dict[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        del name, deadline
        return current

    def _raise_for_runtime_error(
        self,
        name: str,
        harness: Harness,
        state: AgentState,
    ) -> None:
        del name, harness, state
        if self._runtime_error is not None:
            raise self._runtime_error


def _result(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["herdr"],
        0,
        json.dumps({"id": "test", "result": payload}),
        "",
    )


def _error(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["herdr"],
        1,
        "",
        json.dumps({"error": {"code": code}}),
    )


def _text(output: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["herdr"], 0, output, "")


def _agent(
    workspace: Path,
    state: AgentState,
    sequence: int,
    **overrides: object,
) -> dict[str, object]:
    agent: dict[str, object] = {
        "name": "owned-codex",
        "agent": "codex",
        "agent_status": state.value,
        "pane_id": "w1:p2",
        "workspace_id": "w1",
        "cwd": str(workspace),
        "foreground_cwd": str(workspace),
        "state_change_seq": sequence,
        "agent_session": {"value": "session-1"},
    }
    agent.update(overrides)
    return agent


def _runtime(
    workspace: Path,
    phase: AttemptPhase,
    *,
    baseline: int | None = 10,
    accepted: int | None = 11,
    state_change: int | None = 12,
    agent_state: AgentState | None = None,
) -> AttemptRuntime:
    return AttemptRuntime(
        "owned-codex",
        "w1:p2",
        "w1",
        str(workspace),
        "session-1",
        baseline,
        accepted,
        state_change,
        phase,
        agent_state=agent_state,
    )


def _context(progress: list[AttemptProgress] | None = None) -> DispatchContext:
    return DispatchContext(
        PlacementTarget.TAB,
        "Recover",
        "recover-1",
        attempt_progress=None if progress is None else progress.append,
    )


def _recover(
    workspace: Path,
    runner: FakeRunner,
    runtime: AttemptRuntime,
    *,
    progress: list[AttemptProgress] | None = None,
    host: FakeHost | None = None,
):
    host = host or FakeHost(workspace, runner)
    return recover_turn(
        host,
        Harness.CODEX,
        timeout_seconds=30,
        agent_name="owned-codex",
        context=_context(progress),
        runtime=runtime,
    )


def test_recover_turn_adopts_settled_turn(tmp_path: Path) -> None:
    runner = FakeRunner([{"agent": _agent(tmp_path, AgentState.DONE, 12)}])
    progress: list[AttemptProgress] = []

    outcome = _recover(
        tmp_path,
        runner,
        _runtime(tmp_path, AttemptPhase.SETTLED, agent_state=AgentState.DONE),
        progress=progress,
    )

    assert outcome.state is AgentState.DONE
    assert outcome.error_code is None
    assert outcome.agent_settled is True
    assert outcome.member_reused is True
    assert outcome.pane_id == "w1:p2"
    assert [event.phase for event in progress] == [AttemptPhase.RECEIPT_OBSERVED]
    assert runner.calls == [["herdr", "agent", "get", "owned-codex"]]


def test_recover_turn_adopts_blocked_turn(tmp_path: Path) -> None:
    runner = FakeRunner([{"agent": _agent(tmp_path, AgentState.BLOCKED, 12)}])

    outcome = _recover(
        tmp_path,
        runner,
        _runtime(tmp_path, AttemptPhase.SETTLED, agent_state=AgentState.BLOCKED),
    )

    assert outcome.state is AgentState.BLOCKED
    assert outcome.error_code == "agent_blocked"


def test_recover_turn_skips_duplicate_receipt_progress(tmp_path: Path) -> None:
    runner = FakeRunner([{"agent": _agent(tmp_path, AgentState.DONE, 12)}])
    progress: list[AttemptProgress] = []

    outcome = _recover(
        tmp_path,
        runner,
        _runtime(tmp_path, AttemptPhase.RECEIPT_OBSERVED, agent_state=AgentState.DONE),
        progress=progress,
    )

    assert outcome.state is AgentState.DONE
    assert outcome.error_code is None
    assert progress == []


def test_recover_turn_without_runtime_reports_unaccepted_lease(tmp_path: Path) -> None:
    runner = FakeRunner([])
    runtime = AttemptRuntime(
        "owned-codex", None, None, None, None, None, None, None, AttemptPhase.CLAIMED
    )

    outcome = _recover(tmp_path, runner, runtime)

    assert outcome.state is AgentState.UNKNOWN
    assert outcome.error_code == "lease_expired_unaccepted"
    assert outcome.agent_settled is False
    assert runner.calls == []


def test_recover_turn_rejects_unsubmitted_baseline(tmp_path: Path) -> None:
    runner = FakeRunner([{"agent": _agent(tmp_path, AgentState.IDLE, 10)}])

    outcome = _recover(
        tmp_path,
        runner,
        _runtime(tmp_path, AttemptPhase.CLAIMED, baseline=10, accepted=None),
    )

    assert outcome.error_code == "lease_expired_unaccepted"
    assert outcome.agent_settled is False


@pytest.mark.parametrize(
    ("phase", "agent"),
    (
        pytest.param(
            AttemptPhase.PROMPT_ACCEPTED,
            {"state": AgentState.DONE, "sequence": 11},
            id="prompt-accepted-phase",
        ),
        pytest.param(
            AttemptPhase.RUNTIME_ACQUIRED,
            {"state": AgentState.WORKING, "sequence": 10},
            id="runtime-acquired-working",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {"state": AgentState.DONE, "sequence": 99},
            id="sequence-drift",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {"state": AgentState.WORKING, "sequence": 12},
            id="unsettled-state",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {"state": AgentState.DONE, "sequence": 12, "pane_id": "w1:other"},
            id="pane-mismatch",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {"state": AgentState.DONE, "sequence": 12, "workspace_id": "w2"},
            id="workspace-mismatch",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {"state": AgentState.DONE, "sequence": 12, "agent": "droid"},
            id="harness-mismatch",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {"state": AgentState.DONE, "sequence": 12, "name": "other-agent"},
            id="name-mismatch",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {
                "state": AgentState.DONE,
                "sequence": 12,
                "agent_session": {"value": "session-2"},
            },
            id="session-mismatch",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {"state": AgentState.DONE, "sequence": 12, "cwd": "/elsewhere"},
            id="cwd-mismatch",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {"state": AgentState.DONE, "sequence": -1},
            id="negative-sequence",
        ),
        pytest.param(
            AttemptPhase.SETTLED,
            {"state": AgentState.DONE, "sequence": True},
            id="boolean-sequence",
        ),
    ),
)
def test_recover_turn_rejects_unsafe_adoption(
    tmp_path: Path,
    phase: AttemptPhase,
    agent: dict[str, object],
) -> None:
    payload = _agent(
        tmp_path,
        agent["state"],
        agent["sequence"],
        **{key: value for key, value in agent.items() if key not in {"state", "sequence"}},
    )
    runner = FakeRunner([{"agent": payload}])
    progress: list[AttemptProgress] = []

    outcome = _recover(
        tmp_path,
        runner,
        _runtime(tmp_path, phase, agent_state=AgentState.DONE),
        progress=progress,
    )

    assert outcome.state is AgentState.UNKNOWN
    assert outcome.error_code == "unsafe_turn_adoption"
    assert outcome.agent_settled is False
    assert progress == []
    assert all(call[0:3] == ["herdr", "agent", "get"] for call in runner.calls)


def test_recover_turn_rejects_missing_agent_payload(tmp_path: Path) -> None:
    runner = FakeRunner([{"unexpected": {}}])

    outcome = _recover(
        tmp_path,
        runner,
        _runtime(tmp_path, AttemptPhase.SETTLED, agent_state=AgentState.DONE),
    )

    assert outcome.error_code == "unsafe_turn_adoption"
    assert outcome.agent_settled is False


def test_recover_turn_rejects_sequence_jump_during_confirmation(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            {"agent": _agent(tmp_path, AgentState.DONE, 12)},
            {"agent": _agent(tmp_path, AgentState.DONE, 13)},
        ]
    )
    host = FakeHost(tmp_path, runner, settled_confirmation_polls=1)

    outcome = _recover(
        tmp_path,
        runner,
        _runtime(tmp_path, AttemptPhase.SETTLED, agent_state=AgentState.DONE),
        host=host,
    )

    assert outcome.error_code == "unsafe_turn_adoption"
    assert outcome.agent_settled is False
    assert len(runner.calls) == 2


def test_recover_turn_returns_settled_runtime_error(tmp_path: Path) -> None:
    runner = FakeRunner([{"agent": _agent(tmp_path, AgentState.DONE, 12)}])
    host = FakeHost(
        tmp_path,
        runner,
        runtime_error=TransportError(
            "agent_crashed", summary="exit 1", agent_settled=True
        ),
    )

    outcome = _recover(
        tmp_path,
        runner,
        _runtime(tmp_path, AttemptPhase.SETTLED, agent_state=AgentState.DONE),
        host=host,
    )

    assert outcome.state is AgentState.DONE
    assert outcome.error_code == "agent_crashed"
    assert outcome.error_summary == "exit 1"
    assert outcome.agent_settled is True


def test_recover_turn_treats_unsettled_runtime_error_as_unsafe(tmp_path: Path) -> None:
    runner = FakeRunner([{"agent": _agent(tmp_path, AgentState.DONE, 12)}])
    host = FakeHost(
        tmp_path,
        runner,
        runtime_error=TransportError("agent_crashed", agent_settled=False),
    )

    outcome = _recover(
        tmp_path,
        runner,
        _runtime(tmp_path, AttemptPhase.SETTLED, agent_state=AgentState.DONE),
        host=host,
    )

    assert outcome.error_code == "unsafe_turn_adoption"
    assert outcome.agent_settled is False


def test_submit_blocked_response_returns_settled_state(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            {"type": "ok"},
            {"agent": _agent(tmp_path, AgentState.DONE, 6)},
        ]
    )
    host = FakeHost(tmp_path, runner)
    accepted: list[dict[str, Any]] = []

    state, sequence = submit_blocked_response(
        host,
        "blocked-worker",
        "w1:p2",
        "Approved",
        5,
        30,
        on_acceptance=accepted.append,
    )

    assert (state, sequence) == (AgentState.DONE, 6)
    assert len(accepted) == 1
    assert runner.calls[0][0:3] == ["herdr", "pane", "run"]


@pytest.mark.parametrize("code", sorted(AMBIGUOUS_RESPONSE_SUBMISSION_ERRORS))
def test_submit_blocked_response_reconciles_ambiguous_submission(
    tmp_path: Path, code: str
) -> None:
    runner = FakeRunner(
        [
            {"_error": code},
            {"agent": _agent(tmp_path, AgentState.DONE, 6)},
        ]
    )
    host = FakeHost(tmp_path, runner)

    state, sequence = submit_blocked_response(
        host, "blocked-worker", "w1:p2", "Approved", 5, 30
    )

    assert (state, sequence) == (AgentState.DONE, 6)
    assert sum(call[0:3] == ["herdr", "pane", "run"] for call in runner.calls) == 1


def test_submit_blocked_response_reraises_unambiguous_submission_error(
    tmp_path: Path,
) -> None:
    runner = FakeRunner([{"_error": "permission_denied"}])
    host = FakeHost(tmp_path, runner)

    with pytest.raises(TransportError, match="permission_denied"):
        submit_blocked_response(host, "blocked-worker", "w1:p2", "Approved", 5, 30)

    assert runner.calls == [["herdr", "pane", "run", "w1:p2", "Approved"]]


def test_submit_blocked_response_without_acceptance_is_unsafe(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            {"type": "ok"},
            {"agent": _agent(tmp_path, AgentState.WORKING, 5)},
        ]
    )
    host = FakeHost(tmp_path, runner)

    with pytest.raises(TransportError, match="unsafe_turn_adoption"):
        submit_blocked_response(host, "blocked-worker", "w1:p2", "Approved", 5, 0)


def test_submit_blocked_response_reraises_reconciliation_error_after_acceptance(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        [
            {"_error": "timeout"},
            {"agent": _agent(tmp_path, AgentState.WORKING, 6)},
        ]
    )
    host = FakeHost(tmp_path, runner)

    with pytest.raises(TransportError, match="herdr_timeout"):
        submit_blocked_response(host, "blocked-worker", "w1:p2", "Approved", 5, 0)
