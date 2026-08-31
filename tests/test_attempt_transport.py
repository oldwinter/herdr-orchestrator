from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from herdr_orchestrator.herdr import HerdrTransport
from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    AttemptRuntime,
    DispatchContext,
    Harness,
    JobState,
    NewJob,
    PlacementTarget,
)
from herdr_orchestrator.runner import OperationInterrupted
from herdr_orchestrator.store import Store


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


class CrashAfterSubmissionRunner(FakeRunner):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        if argv[0:3] == ["herdr", "pane", "run"]:
            self.calls.append(argv)
            raise OperationInterrupted("after_atomic_submission")
        return super().__call__(argv, cwd=cwd, timeout=timeout)


def test_dispatch_reports_each_durable_lifecycle_phase() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runner = FakeRunner(
            [
                {"_error": "agent_not_found"},
                {"root_pane": {"pane_id": "w1:p2"}, "tab": {"tab_id": "w1:t2"}},
                {"process_info": {"shell_pid": 42, "foreground_processes": [{"pid": 42}]}},
                {"agent": _minimal_agent(AgentState.IDLE, 1, interactive=True)},
                {"agent": _minimal_agent(AgentState.IDLE, 1, interactive=True)},
                {"agent": _minimal_agent(AgentState.IDLE, 1)},
                {"agent": _minimal_agent(AgentState.DONE, 3)},
            ]
        )
        progress: list[AttemptProgress] = []
        transport = _transport(workspace, runner)

        outcome = transport.dispatch(
            Harness.CODEX,
            "review",
            timeout_seconds=30,
            context=DispatchContext(
                PlacementTarget.TAB,
                "Review",
                "review-1",
                attempt_progress=progress.append,
            ),
        )

    assert outcome.state is AgentState.DONE
    assert [event.phase for event in progress] == [
        AttemptPhase.RUNTIME_ACQUIRED,
        AttemptPhase.PROMPT_ACCEPTED,
        AttemptPhase.SETTLED,
        AttemptPhase.RECEIPT_OBSERVED,
    ]
    assert progress[0].prompt_baseline_sequence == 1
    assert progress[1].prompt_accepted_sequence == 3
    assert progress[2].state_change_sequence == 3


def test_blocked_response_reports_each_durable_lifecycle_phase() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runner = FakeRunner(
            [
                {"agent": _minimal_agent(AgentState.BLOCKED, 5)},
                {"type": "ok"},
                {"agent": _minimal_agent(AgentState.DONE, 6)},
            ]
        )
        progress: list[AttemptProgress] = []

        outcome = _transport(workspace, runner).respond(
            "blocked-worker",
            Harness.CODEX,
            "Approve this local action.",
            timeout_seconds=30,
            context=DispatchContext(
                PlacementTarget.TAB,
                "Resume",
                "resume-1",
                attempt_progress=progress.append,
            ),
        )

    assert outcome.state is AgentState.DONE
    assert [event.phase for event in progress] == [
        AttemptPhase.RUNTIME_ACQUIRED,
        AttemptPhase.PROMPT_ACCEPTED,
        AttemptPhase.SETTLED,
        AttemptPhase.RECEIPT_OBSERVED,
    ]
    assert progress[0].prompt_baseline_sequence == 5
    assert progress[1].prompt_accepted_sequence == 6
    assert progress[2].state_change_sequence == 6
    submissions = [
        call
        for call in runner.calls
        if call[0:3]
        in (
            ["herdr", "pane", "run"],
            ["herdr", "pane", "send-text"],
            ["herdr", "agent", "send-keys"],
        )
    ]
    assert submissions == [["herdr", "pane", "run", "w1:p9", "Approve this local action."]]


def test_blocked_response_has_one_physical_submission_crash_window() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runner = CrashAfterSubmissionRunner([{"agent": _minimal_agent(AgentState.BLOCKED, 5)}])
        progress: list[AttemptProgress] = []

        with pytest.raises(OperationInterrupted, match="after_atomic_submission"):
            _transport(workspace, runner).respond(
                "blocked-worker",
                Harness.CODEX,
                "Approved",
                timeout_seconds=30,
                context=DispatchContext(
                    PlacementTarget.TAB,
                    "Resume",
                    "resume-crash",
                    attempt_progress=progress.append,
                ),
            )

    mutations = [
        call
        for call in runner.calls
        if call[0:3]
        in (
            ["herdr", "pane", "run"],
            ["herdr", "pane", "send-text"],
            ["herdr", "agent", "send-keys"],
        )
    ]
    assert mutations == [["herdr", "pane", "run", "w1:p9", "Approved"]]
    assert [event.phase for event in progress] == [AttemptPhase.RUNTIME_ACQUIRED]


def test_recovery_does_not_adopt_accepted_turn_without_turn_identity() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runtime = AttemptRuntime(
            "owned-codex",
            "w1:p2",
            "w1",
            str(workspace),
            "session-1",
            10,
            11,
            11,
            AttemptPhase.PROMPT_ACCEPTED,
        )
        runner = FakeRunner(
            [
                {"agent": _agent(workspace, AgentState.WORKING, 11)},
            ]
        )
        progress: list[AttemptProgress] = []
        context = DispatchContext(
            PlacementTarget.TAB,
            "Recover",
            "recover-1",
            attempt_progress=progress.append,
        )
        transport = HerdrTransport(
            "example",
            workspace,
            environ={
                "HERDR_ENV": "1",
                "HERDR_PANE_ID": "w1:p1",
                "HERDR_WORKSPACE_ID": "w1",
            },
            runner=runner,
            sleeper=lambda _: None,
            settled_confirmation_polls=0,
            inspect_runtime_errors=False,
        )

        outcome = transport.recover(
            Harness.CODEX,
            "must not be sent",
            timeout_seconds=30,
            agent_name="owned-codex",
            context=context,
            runtime=runtime,
        )

    assert outcome.state is AgentState.UNKNOWN
    assert outcome.error_code == "unsafe_turn_adoption"
    assert outcome.agent_settled is False
    assert progress == []
    assert all(call[0:3] == ["herdr", "agent", "get"] for call in runner.calls)


def test_recovery_identity_mismatch_enters_attention_without_sending_input() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runtime = AttemptRuntime(
            "owned-codex",
            "w1:p2",
            "w1",
            str(workspace),
            "session-1",
            10,
            11,
            11,
            AttemptPhase.PROMPT_ACCEPTED,
        )
        mismatched = _agent(workspace, AgentState.WORKING, 11)
        mismatched["pane_id"] = "w1:other"
        runner = FakeRunner([{"agent": mismatched}])
        transport = HerdrTransport(
            "example",
            workspace,
            environ={
                "HERDR_ENV": "1",
                "HERDR_PANE_ID": "w1:p1",
                "HERDR_WORKSPACE_ID": "w1",
            },
            runner=runner,
            sleeper=lambda _: None,
            settled_confirmation_polls=0,
            inspect_runtime_errors=False,
        )

        outcome = transport.recover(
            Harness.CODEX,
            "must not be sent",
            timeout_seconds=30,
            agent_name="owned-codex",
            context=DispatchContext(PlacementTarget.TAB, "Recover", "recover-2"),
            runtime=runtime,
        )

    assert outcome.error_code == "unsafe_turn_adoption"
    assert runner.calls == [["herdr", "agent", "get", "owned-codex"]]


def test_recovery_of_claimed_operation_without_runtime_does_not_query_herdr() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runner = FakeRunner([])

        outcome = _transport(workspace, runner).recover(
            Harness.CODEX,
            "must not be sent",
            timeout_seconds=30,
            agent_name="owned-codex",
            context=DispatchContext(PlacementTarget.TAB, "Recover", "recover-claimed"),
            runtime=AttemptRuntime(
                "owned-codex",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                AttemptPhase.CLAIMED,
            ),
        )

    assert outcome.error_code == "lease_expired_unaccepted"
    assert outcome.agent_settled is False
    assert runner.calls == []


def test_recovery_rejects_unrelated_later_turn_sequence() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runtime = AttemptRuntime(
            "owned-codex",
            "w1:p2",
            "w1",
            str(workspace),
            "session-1",
            10,
            11,
            11,
            AttemptPhase.PROMPT_ACCEPTED,
        )
        runner = FakeRunner([{"agent": _agent(workspace, AgentState.DONE, 99)}])
        progress: list[AttemptProgress] = []

        outcome = _transport(workspace, runner).recover(
            Harness.CODEX,
            "must not be sent",
            timeout_seconds=30,
            agent_name="owned-codex",
            context=DispatchContext(
                PlacementTarget.TAB,
                "Recover",
                "recover-later-turn",
                attempt_progress=progress.append,
            ),
            runtime=runtime,
        )

    assert outcome.error_code == "unsafe_turn_adoption"
    assert progress == []
    assert runner.calls == [["herdr", "agent", "get", "owned-codex"]]


def test_recovery_rejects_sequence_jump_during_stable_confirmation() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runtime = AttemptRuntime(
            "owned-codex",
            "w1:p2",
            "w1",
            str(workspace),
            "session-1",
            10,
            11,
            12,
            AttemptPhase.SETTLED,
        )
        runner = FakeRunner(
            [
                {"agent": _agent(workspace, AgentState.DONE, 12)},
                {"agent": _agent(workspace, AgentState.DONE, 99)},
            ]
        )
        progress: list[AttemptProgress] = []

        outcome = HerdrTransport(
            "example",
            workspace,
            environ={
                "HERDR_ENV": "1",
                "HERDR_PANE_ID": "w1:p1",
                "HERDR_WORKSPACE_ID": "w1",
            },
            runner=runner,
            sleeper=lambda _: None,
            settled_confirmation_polls=1,
            inspect_runtime_errors=False,
        ).recover(
            Harness.CODEX,
            "must not be sent",
            timeout_seconds=30,
            agent_name="owned-codex",
            context=DispatchContext(
                PlacementTarget.TAB,
                "Recover",
                "recover-confirmation-jump",
                attempt_progress=progress.append,
            ),
            runtime=runtime,
        )

    assert outcome.error_code == "unsafe_turn_adoption"
    assert outcome.agent_settled is False
    assert progress == []
    assert runner.calls == [
        ["herdr", "agent", "get", "owned-codex"],
        ["herdr", "agent", "get", "owned-codex"],
    ]


def test_recovery_commits_an_exact_durable_settlement() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runtime = AttemptRuntime(
            "owned-codex",
            "w1:p2",
            "w1",
            str(workspace),
            "session-1",
            10,
            11,
            12,
            AttemptPhase.SETTLED,
        )
        runner = FakeRunner([{"agent": _agent(workspace, AgentState.DONE, 12)}])
        progress: list[AttemptProgress] = []

        outcome = _transport(workspace, runner).recover(
            Harness.CODEX,
            "must not be sent",
            timeout_seconds=30,
            agent_name="owned-codex",
            context=DispatchContext(
                PlacementTarget.TAB,
                "Recover",
                "recover-durable-settlement",
                attempt_progress=progress.append,
            ),
            runtime=runtime,
        )

    assert outcome.state is AgentState.DONE
    assert outcome.error_code is None
    assert outcome.agent_settled is True
    assert [event.phase for event in progress] == [AttemptPhase.RECEIPT_OBSERVED]
    assert progress[0].state_change_sequence == 12


def test_recovery_without_migrated_baseline_enters_attention() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runtime = AttemptRuntime(
            "owned-codex",
            "w1:p2",
            "w1",
            str(workspace),
            None,
            None,
            None,
            None,
            AttemptPhase.RUNTIME_ACQUIRED,
        )
        blocked = _agent(workspace, AgentState.BLOCKED, 50)
        blocked["agent_session"] = None
        runner = FakeRunner([{"agent": blocked}])

        outcome = _transport(workspace, runner).recover(
            Harness.CODEX,
            "must not be sent",
            timeout_seconds=30,
            agent_name="owned-codex",
            context=DispatchContext(PlacementTarget.TAB, "Recover", "recover-migrated"),
            runtime=runtime,
        )

    assert outcome.error_code == "unsafe_turn_adoption"
    assert outcome.agent_settled is False
    assert runner.calls == [["herdr", "agent", "get", "owned-codex"]]


@pytest.mark.parametrize(
    ("detection_output", "error_code"),
    [
        ("API Error: 403 status code (no body)", "agent_auth_failed"),
        ("Error: unknown model example-invalid", "agent_model_invalid"),
        ("API failed after 5 retries: provider unavailable", "agent_provider_failed"),
    ],
)
def test_recovery_preserves_settled_fatal_runtime_errors(
    detection_output: str,
    error_code: str,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        runtime = AttemptRuntime(
            "owned-codex",
            "w1:p2",
            "w1",
            str(workspace),
            "session-1",
            10,
            11,
            12,
            AttemptPhase.SETTLED,
        )
        runner = FakeRunner(
            [
                {"agent": _agent(workspace, AgentState.DONE, 12)},
                detection_output,
            ]
        )
        progress: list[AttemptProgress] = []

        outcome = HerdrTransport(
            "example",
            workspace,
            environ={
                "HERDR_ENV": "1",
                "HERDR_PANE_ID": "w1:p1",
                "HERDR_WORKSPACE_ID": "w1",
            },
            runner=runner,
            sleeper=lambda _: None,
            settled_confirmation_polls=0,
            inspect_runtime_errors=True,
        ).recover(
            Harness.CODEX,
            "must not be sent",
            timeout_seconds=30,
            agent_name="owned-codex",
            context=DispatchContext(
                PlacementTarget.TAB,
                "Recover",
                "recover-fatal",
                attempt_progress=progress.append,
            ),
            runtime=runtime,
        )

    assert outcome.error_code == error_code
    assert outcome.agent_settled is True
    assert outcome.error_summary is not None
    assert progress == []
    assert runner.calls == [
        ["herdr", "agent", "get", "owned-codex"],
        [
            "herdr",
            "agent",
            "read",
            "owned-codex",
            "--source",
            "detection",
            "--lines",
            "80",
        ],
    ]


def test_settled_fatal_recovery_follows_store_retry_policy() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        store = Store(workspace / "state.db")
        store.initialize()
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            store.enqueue(
                NewJob(
                    "example",
                    "Recover settled fatal",
                    Harness.CODEX,
                    "must not be sent",
                    "recover-settled-fatal",
                    2,
                )
            )
            claimed = store.claim(
                "example",
                limit=1,
                lease_seconds=10,
                slot_names={Harness.CODEX.value: ("owned-codex",)},
            )[0]
            store.record_attempt_progress(
                claimed,
                AttemptProgress(
                    AttemptPhase.RUNTIME_ACQUIRED,
                    claimed.agent_name,
                    pane_id="w1:p2",
                    herdr_workspace_id="w1",
                    execution_path=str(workspace),
                    agent_session_id="session-1",
                    prompt_baseline_sequence=10,
                    state_change_sequence=10,
                    agent_state=AgentState.IDLE,
                ),
            )
            store.record_attempt_progress(
                claimed,
                AttemptProgress(
                    AttemptPhase.PROMPT_ACCEPTED,
                    claimed.agent_name,
                    pane_id="w1:p2",
                    prompt_baseline_sequence=10,
                    prompt_accepted_sequence=11,
                    state_change_sequence=11,
                    agent_state=AgentState.WORKING,
                ),
            )
            store.record_attempt_progress(
                claimed,
                AttemptProgress(
                    AttemptPhase.SETTLED,
                    claimed.agent_name,
                    pane_id="w1:p2",
                    state_change_sequence=12,
                    agent_state=AgentState.DONE,
                    agent_settled=True,
                ),
            )
        with patch("herdr_orchestrator.store.time.time", return_value=111.0):
            recovered = store.claim(
                "example",
                limit=1,
                lease_seconds=10,
                slot_names={Harness.CODEX.value: ("owned-codex",)},
            )[0]
            assert recovered.runtime is not None
            runner = FakeRunner(
                [
                    {"agent": _agent(workspace, AgentState.DONE, 12)},
                    "API Error: 403 status code (no body)",
                ]
            )
            outcome = HerdrTransport(
                "example",
                workspace,
                environ={
                    "HERDR_ENV": "1",
                    "HERDR_PANE_ID": "w1:p1",
                    "HERDR_WORKSPACE_ID": "w1",
                },
                runner=runner,
                sleeper=lambda _: None,
                settled_confirmation_polls=0,
                inspect_runtime_errors=True,
            ).recover(
                Harness.CODEX,
                recovered.prompt,
                timeout_seconds=30,
                agent_name=recovered.agent_name,
                context=DispatchContext(
                    recovered.placement,
                    recovered.title,
                    recovered.dedupe_key,
                    correlation_id=recovered.correlation_id,
                    attempt_progress=lambda progress: store.record_attempt_progress(
                        recovered, progress
                    ),
                ),
                runtime=recovered.runtime,
            )
            state = store.record_outcome(recovered, outcome)
        job = store.jobs("example")[0]

    assert outcome.error_code == "agent_auth_failed"
    assert outcome.agent_settled is True
    assert state is JobState.PENDING
    assert job["state"] == JobState.PENDING.value
    assert job["attempt_phase"] == AttemptPhase.ABANDONED.value
    assert job["error_code"] == "agent_auth_failed"
    assert job["agent_settled"] is True


def _agent(workspace: Path, state: AgentState, sequence: int) -> dict[str, object]:
    return {
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


def _minimal_agent(
    state: AgentState,
    sequence: int,
    *,
    interactive: bool | None = None,
) -> dict[str, object]:
    agent: dict[str, object] = {
        "agent": "codex",
        "agent_status": state.value,
        "pane_id": "w1:p2" if state is not AgentState.BLOCKED else "w1:p9",
        "state_change_seq": sequence,
    }
    if interactive is not None:
        agent["interactive_ready"] = interactive
    return agent


def _transport(workspace: Path, runner: FakeRunner) -> HerdrTransport:
    return HerdrTransport(
        "example",
        workspace,
        environ={
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_WORKSPACE_ID": "w1",
        },
        runner=runner,
        sleeper=lambda _: None,
        settled_confirmation_polls=0,
        inspect_runtime_errors=False,
    )


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
