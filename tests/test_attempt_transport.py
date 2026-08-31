from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from herdr_orchestrator.herdr import HerdrTransport
from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    AttemptRuntime,
    DispatchContext,
    Harness,
    PlacementTarget,
)
from herdr_orchestrator.runner import OperationInterrupted


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
        if argv[0:3] == ["herdr", "pane", "send-text"]:
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
    submissions = [
        call
        for call in runner.calls
        if call[0:3] in (["herdr", "pane", "send-text"], ["herdr", "agent", "send-keys"])
    ]
    assert submissions == [["herdr", "pane", "send-text", "w1:p9", "Approve this local action.\n"]]


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
        if call[0:3] in (["herdr", "pane", "send-text"], ["herdr", "agent", "send-keys"])
    ]
    assert mutations == [["herdr", "pane", "send-text", "w1:p9", "Approved\n"]]
    assert [event.phase for event in progress] == [AttemptPhase.RUNTIME_ACQUIRED]


def test_recovers_matching_accepted_turn_without_sending_input() -> None:
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
                {"agent": _agent(workspace, AgentState.DONE, 12)},
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

    assert outcome.state is AgentState.DONE, (outcome, progress, runner.calls)
    assert outcome.error_code is None
    assert [event.phase for event in progress] == [
        AttemptPhase.SETTLED,
        AttemptPhase.RECEIPT_OBSERVED,
    ]
    assert progress[0].state_change_sequence == 12
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


def test_recovery_without_migrated_baseline_proves_unchanged_blocked_state() -> None:
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
            AttemptPhase.CLAIMED,
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

    assert outcome.error_code == "lease_expired_unaccepted"
    assert runner.calls == [["herdr", "agent", "get", "owned-codex"]]


def test_recovery_reads_only_bounded_detection_output_for_fatal_signals() -> None:
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
            "must not be sent",
            timeout_seconds=30,
            agent_name="owned-codex",
            context=DispatchContext(PlacementTarget.TAB, "Recover", "recover-fatal"),
            runtime=runtime,
        )

    assert outcome.error_code == "unsafe_turn_adoption"
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
