from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.herdr import HerdrTransport
from herdr_orchestrator.model import (
    AgentState,
    DispatchContext,
    Harness,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
)
from herdr_orchestrator.protocol import TransportError


class FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if not self.responses:
            raise AssertionError(f"unexpected call: {argv}")
        return self.responses.pop(0)


class HerdrTransportTests(unittest.TestCase):
    def test_closes_only_the_owned_pane_for_a_tab_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "name": "ho-codex-owned",
                                "agent": "codex",
                                "agent_status": "done",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "workspace_id": "w1",
                                "tab_id": "w1:t2",
                                "pane_id": "w1:p3",
                            }
                        }
                    ),
                    _result({"type": "ok"}),
                ]
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
            )

            action = transport.close_agent_terminal(
                "ho-codex-owned",
                PlacementTarget.TAB,
                expected_pane_id="w1:p3",
                dry_run=False,
            )

        self.assertEqual(action["action"], "closed")
        self.assertEqual(
            runner.calls[1],
            ["herdr", "pane", "close", "w1:p3"],
        )

    def test_tab_agent_moved_to_a_user_tab_still_closes_only_its_pane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "name": "ho-codex-owned",
                                "agent": "codex",
                                "agent_status": "done",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "workspace_id": "w1",
                                "tab_id": "w1:user-tab",
                                "pane_id": "w1:p3",
                            }
                        }
                    ),
                    _result({"type": "ok"}),
                ]
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
            )

            transport.close_agent_terminal(
                "ho-codex-owned",
                PlacementTarget.TAB,
                expected_pane_id="w1:p3",
                dry_run=False,
            )

        self.assertEqual(runner.calls[1], ["herdr", "pane", "close", "w1:p3"])

    def test_refuses_cleanup_when_the_owned_agent_moved_to_another_pane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "name": "ho-codex-owned",
                                "agent": "codex",
                                "agent_status": "done",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "workspace_id": "w1",
                                "tab_id": "w1:t2",
                                "pane_id": "w1:p8",
                            }
                        }
                    ),
                ]
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
            )

            with self.assertRaisesRegex(TransportError, "agent_pane_mismatch"):
                transport.close_agent_terminal(
                    "ho-codex-owned",
                    PlacementTarget.TAB,
                    expected_pane_id="w1:p3",
                    dry_run=False,
                )

        self.assertEqual(len(runner.calls), 1)

    def test_starts_and_prompts_new_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            sleeps: list[float] = []
            runner = FakeRunner(
                [
                    _error("agent_not_found"),
                    _result(
                        {
                            "root_pane": {"pane_id": "w1:p2"},
                            "tab": {"tab_id": "w1:t2"},
                        }
                    ),
                    _result(
                        {
                            "process_info": {
                                "shell_pid": 42,
                                "foreground_processes": [{"pid": 42, "name": "zsh"}],
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "pane_id": "w1:p2",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "pane_id": "w1:p2",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "idle",
                                "pane_id": "w1:p2",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "done",
                                "pane_id": "w1:p2",
                                "state_change_seq": 3,
                            }
                        }
                    ),
                ]
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
                sleeper=sleeps.append,
                settled_confirmation_polls=0,
                inspect_runtime_errors=False,
            )

            outcome = transport.dispatch(Harness.CODEX, "review", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        self.assertFalse(outcome.member_reused)
        self.assertEqual(outcome.pane_id, "w1:p2")
        self.assertIn("--no-focus", runner.calls[1])
        self.assertEqual(runner.calls[3][0:4], ["herdr", "agent", "start", outcome.agent_name])
        start_timeout = runner.calls[3][runner.calls[3].index("--timeout") + 1]
        self.assertLessEqual(int(start_timeout), 30_000)
        self.assertEqual(runner.calls[6][0:4], ["herdr", "agent", "prompt", outcome.agent_name])
        self.assertIn("--wait", runner.calls[6])
        self.assertTrue(all(timeout is None or timeout <= 30 for timeout in runner.timeouts))
        self.assertIn(3, sleeps)

    def test_reuses_matching_settled_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.DROID, "inspect", timeout_seconds=30)

        self.assertTrue(outcome.member_reused)
        self.assertEqual(len(runner.calls), 3)
        self.assertIn("--wait", runner.calls[2])

    def test_rejects_reusable_agent_from_another_herdr_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "workspace_id": "w2",
                                "pane_id": "w2:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                ]
            )
            transport = HerdrTransport(
                "example",
                workspace,
                environ={"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_WORKSPACE_ID": "w1"},
                runner=runner,
                settled_confirmation_polls=0,
                inspect_runtime_errors=False,
            )

            outcome = transport.dispatch(Harness.DROID, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.error_code, "agent_workspace_mismatch")
        self.assertEqual(outcome.state, AgentState.UNKNOWN)
        self.assertEqual(len(runner.calls), 1)

    def test_rejects_settled_prompt_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 10,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 10,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 10,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.GROK, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.UNKNOWN)
        self.assertEqual(outcome.error_code, "agent_turn_not_observed")

    def test_waits_for_interactive_ready_before_prompting_new_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _error("agent_not_found"),
                    _result(
                        {
                            "root_pane": {"pane_id": "w1:p7"},
                            "tab": {"tab_id": "w1:t7"},
                        }
                    ),
                    _result(
                        {
                            "process_info": {
                                "shell_pid": 42,
                                "foreground_processes": [{"pid": 42, "name": "zsh"}],
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "interactive_ready": False,
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "done",
                                "pane_id": "w1:p7",
                                "state_change_seq": 3,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.GROK, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        prompt_call = next(
            index
            for index, call in enumerate(runner.calls)
            if call[0:3] == ["herdr", "agent", "prompt"]
        )
        readiness_gets = [
            call for call in runner.calls[:prompt_call] if call[0:3] == ["herdr", "agent", "get"]
        ]
        self.assertEqual(len(readiness_gets), 4)

    def test_rechecks_transient_startup_block_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _error("agent_not_found"),
                    _result(
                        {
                            "root_pane": {"pane_id": "w1:p7"},
                            "tab": {"tab_id": "w1:t7"},
                        }
                    ),
                    _result(
                        {
                            "process_info": {
                                "shell_pid": 42,
                                "foreground_processes": [{"pid": 42, "name": "zsh"}],
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "blocked",
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "Startup still settling.\n",
                        "",
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "pane_id": "w1:p7",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "idle",
                                "pane_id": "w1:p7",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "done",
                                "pane_id": "w1:p7",
                                "state_change_seq": 4,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.CLAUDE, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        self.assertIsNone(outcome.error_code)

    def test_requires_herdr_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transport = HerdrTransport(
                "example",
                Path(temporary),
                environ={},
                runner=FakeRunner([]),
                sleeper=lambda _: None,
                settled_confirmation_polls=0,
                inspect_runtime_errors=False,
            )

            outcome = transport.dispatch(Harness.PI, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.error_code, "not_in_herdr")
        self.assertFalse(outcome.member_reused)

    def test_recovers_agent_that_becomes_ready_after_start_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _error("agent_not_found"),
                    _result(
                        {
                            "root_pane": {"pane_id": "w1:p7"},
                            "tab": {"tab_id": "w1:t7"},
                        }
                    ),
                    _result(
                        {
                            "process_info": {
                                "shell_pid": 42,
                                "foreground_processes": [{"pid": 42, "name": "zsh"}],
                            }
                        }
                    ),
                    _error("agent_not_ready"),
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "idle",
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "done",
                                "pane_id": "w1:p7",
                                "state_change_seq": 3,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.HERMES, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        wait_call = next(
            call
            for call in runner.calls
            if call[0:4] == ["herdr", "agent", "wait", outcome.agent_name]
        )
        self.assertLessEqual(int(wait_call[-1]), 30_000)

    def test_waits_when_successful_agent_start_is_still_working(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _error("agent_not_found"),
                    _result(
                        {
                            "root_pane": {"pane_id": "w1:p7"},
                            "tab": {"tab_id": "w1:t7"},
                        }
                    ),
                    _result(
                        {
                            "process_info": {
                                "shell_pid": 42,
                                "foreground_processes": [{"pid": 42, "name": "zsh"}],
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "working",
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "pane_id": "w1:p7",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "pane_id": "w1:p7",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "idle",
                                "pane_id": "w1:p7",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "done",
                                "pane_id": "w1:p7",
                                "state_change_seq": 4,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.CODEX, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        wait_call = next(
            call
            for call in runner.calls
            if call[0:4] == ["herdr", "agent", "wait", outcome.agent_name]
        )
        self.assertLessEqual(int(wait_call[-1]), 30_000)

    def test_preserves_new_agent_tab_when_startup_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _error("agent_not_found"),
                    _result(
                        {
                            "root_pane": {"pane_id": "w1:p7"},
                            "tab": {"tab_id": "w1:t7"},
                        }
                    ),
                    _result(
                        {
                            "process_info": {
                                "shell_pid": 42,
                                "foreground_processes": [{"pid": 42, "name": "zsh"}],
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "blocked",
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "blocked",
                                "interactive_ready": True,
                                "pane_id": "w1:p7",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.CODEX, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.BLOCKED)
        self.assertEqual(outcome.error_code, "agent_blocked")
        self.assertEqual(outcome.pane_id, "w1:p7")
        self.assertFalse(outcome.member_reused)
        self.assertFalse(any(call[0:3] == ["herdr", "tab", "close"] for call in runner.calls))

    def test_polls_after_prompt_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 10,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 10,
                            }
                        }
                    ),
                    _error("agent_prompt_stalled"),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 12,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.DROID, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        self.assertEqual(runner.calls[-1][0:4], ["herdr", "agent", "get", outcome.agent_name])

    def test_prompt_wait_uses_herdr_defaults_then_polls_if_still_working(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 20,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 20,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "working",
                                "pane_id": "w1:p9",
                                "state_change_seq": 21,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 22,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.GROK, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        prompt_call = runner.calls[2]
        self.assertEqual(prompt_call[0:3], ["herdr", "agent", "prompt"])
        self.assertNotIn("--until", prompt_call)
        self.assertIn("--wait", prompt_call)
        prompt_timeout = int(prompt_call[prompt_call.index("--timeout") + 1])
        self.assertGreater(prompt_timeout, 5_000)
        self.assertLessEqual(prompt_timeout, 30_000)

    def test_prompt_timeout_reconciles_an_observed_working_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 20,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 20,
                            }
                        }
                    ),
                    _error("timeout"),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "working",
                                "pane_id": "w1:p9",
                                "state_change_seq": 21,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 22,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.DROID, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        self.assertIsNone(outcome.error_code)

    def test_prompt_timeout_without_sequence_reports_acceptance_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 20,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 20,
                            }
                        }
                    ),
                    _error("herdr_timeout"),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 20,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.DROID, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.UNKNOWN)
        self.assertEqual(outcome.error_code, "prompt_acceptance_timeout")
        self.assertIn("phase=prompt_acceptance", outcome.error_summary or "")
        self.assertIn("state_change_seq=20", outcome.error_summary or "")
        self.assertIn("provision_ready", outcome.phase_timings_ms or {})
        self.assertIn("turn_settlement", outcome.phase_timings_ms or {})
        self.assertIn("total", outcome.phase_timings_ms or {})

    def test_stalled_prompt_fails_fast_when_enter_never_starts_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 30,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 30,
                            }
                        }
                    ),
                    _error("agent_prompt_stalled"),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 30,
                            }
                        }
                    ),
                    _result({"type": "ok"}),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 30,
                            }
                        }
                    ),
                    _result({"type": "ok"}),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 30,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.GROK, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.UNKNOWN)
        self.assertEqual(outcome.error_code, "agent_turn_not_observed")
        self.assertEqual(
            sum(call[0:3] == ["herdr", "agent", "send-keys"] for call in runner.calls),
            2,
        )

    def test_resubmits_enter_when_stalled_prompt_is_still_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 10,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 10,
                            }
                        }
                    ),
                    _error("agent_prompt_stalled"),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 10,
                            }
                        }
                    ),
                    _result({"type": "ok"}),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 12,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(Harness.PI, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        self.assertIn(
            ["herdr", "agent", "send-keys", outcome.agent_name, "enter"],
            runner.calls,
        )

    def test_waits_when_settled_agent_resumes_working(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "working",
                                "pane_id": "w1:p9",
                                "state_change_seq": 3,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 4,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 4,
                            }
                        }
                    ),
                ]
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
                settled_confirmation_polls=2,
                inspect_runtime_errors=False,
            )

            outcome = transport.dispatch(Harness.CODEX, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.state, AgentState.DONE)
        self.assertEqual(
            sum(call[0:3] == ["herdr", "agent", "get"] for call in runner.calls),
            5,
        )

    def test_reports_claude_auth_failure_in_settled_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "⏺ Please run /login · API Error: 403 access denied",
                        "",
                    ),
                ]
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
            )

            outcome = transport.dispatch(Harness.CLAUDE, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.error_code, "agent_auth_failed")
        self.assertEqual(outcome.state, AgentState.UNKNOWN)

    def test_verifies_declared_output_prefix_after_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "Droid ready\n>",
                        "",
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "Task complete\n\u26ec  MOCK-OK harness=pi",
                        "",
                    ),
                ]
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

            outcome = transport.dispatch(
                Harness.PI,
                "inspect",
                timeout_seconds=30,
                context=DispatchContext(
                    placement=PlacementTarget.PANE,
                    title="inspect",
                    task_key="inspect-v1",
                    receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, "MOCK-OK harness=pi"),
                ),
            )

        self.assertTrue(outcome.task_verified)
        self.assertEqual(outcome.state, AgentState.DONE)
        receipt_reads = [call for call in runner.calls if call[0:3] == ["herdr", "agent", "read"]]
        self.assertTrue(receipt_reads)
        self.assertTrue(
            all(call[call.index("--source") + 1] == "recent-unwrapped" for call in receipt_reads)
        )

    def test_does_not_verify_receipt_prefix_mentioned_only_in_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(["herdr"], 0, "", ""),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "Reply with exactly this line: MOCK-OK harness=pi",
                        "",
                    ),
                ]
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

            outcome = transport.dispatch(
                Harness.PI,
                "inspect",
                timeout_seconds=30,
                context=DispatchContext(
                    placement=PlacementTarget.PANE,
                    title="inspect",
                    task_key="inspect-v1",
                    receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, "MOCK-OK harness=pi"),
                ),
            )

        self.assertFalse(outcome.task_verified)
        self.assertIs(outcome.agent_settled, True)
        self.assertEqual(outcome.error_code, "task_receipt_missing")
        self.assertEqual(outcome.state, AgentState.UNKNOWN)

    def test_rejects_output_receipt_that_is_an_exact_prompt_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            transport = HerdrTransport(
                "example",
                workspace,
                environ={
                    "HERDR_ENV": "1",
                    "HERDR_PANE_ID": "w1:p1",
                    "HERDR_WORKSPACE_ID": "w1",
                },
                runner=FakeRunner([]),
            )

            with self.assertRaisesRegex(TransportError, "task_receipt_ambiguous"):
                transport._verify_task_receipt(
                    "pi",
                    TaskReceipt(ReceiptKind.OUTPUT_PREFIX, "MOCK-OK harness=pi"),
                    workspace,
                    prompt="Do the task.\nMOCK-OK harness=pi",
                    output_before="",
                    file_before=None,
                )

    def test_accepts_known_codex_assistant_receipt_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "› Do the task and emit MOCK-OK harness=codex\n" "• MOCK-OK harness=codex",
                        "",
                    )
                ]
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
            )

            verified = transport._verify_task_receipt(
                "codex",
                TaskReceipt(
                    ReceiptKind.OUTPUT_PREFIX,
                    "MOCK-OK harness=codex",
                ),
                workspace,
                prompt="Do the task and emit MOCK-OK harness=codex",
                output_before="",
                file_before=None,
            )

        self.assertTrue(verified)

    def test_does_not_verify_output_prefix_from_a_prior_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            prior_turn_output = subprocess.CompletedProcess(
                ["herdr"],
                0,
                "Earlier work\nMOCK-OK harness=pi",
                "",
            )
            get_calls = 0

            def runner(
                argv: list[str],
                *,
                cwd: str,
                timeout: float | None,
            ) -> subprocess.CompletedProcess[str]:
                nonlocal get_calls
                if argv[0:3] == ["herdr", "agent", "read"]:
                    return prior_turn_output
                if argv[0:3] == ["herdr", "agent", "prompt"]:
                    return _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 9,
                            }
                        }
                    )
                if argv[0:3] == ["herdr", "agent", "get"]:
                    get_calls += 1
                    return _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 8,
                            }
                        }
                    )
                raise AssertionError(f"unexpected call: {argv}")

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

            outcome = transport.dispatch(
                Harness.PI,
                "inspect something else",
                timeout_seconds=30,
                context=DispatchContext(
                    placement=PlacementTarget.PANE,
                    title="inspect",
                    task_key="**********",
                    receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, "MOCK-OK harness=pi"),
                ),
            )

        self.assertFalse(outcome.task_verified)
        self.assertEqual(outcome.error_code, "task_receipt_missing")

    def test_does_not_verify_prompt_echo_receipt_when_agent_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(["herdr"], 0, "", ""),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "blocked",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "MOCK-OK harness=pi\nApproval is required.",
                        "",
                    ),
                ]
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

            outcome = transport.dispatch(
                Harness.PI,
                "MOCK-OK harness=pi\nDo the task, then emit the receipt.",
                timeout_seconds=30,
                context=DispatchContext(
                    placement=PlacementTarget.PANE,
                    title="inspect",
                    task_key="**********",
                    receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, "MOCK-OK harness=pi"),
                ),
            )

        self.assertEqual(outcome.state, AgentState.BLOCKED)
        self.assertEqual(outcome.error_code, "agent_blocked")
        self.assertFalse(outcome.task_verified)

    def test_rejects_unchanged_preexisting_receipt_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "MOCK-HERDR-RECEIPT.txt").write_text(
                "MOCK-WORKTREE-OK\n",
                encoding="utf-8",
            )
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                ]
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

            outcome = transport.dispatch(
                Harness.GROK,
                "write receipt",
                timeout_seconds=30,
                context=DispatchContext(
                    placement=PlacementTarget.PANE,
                    title="write receipt",
                    task_key="receipt-v1",
                    receipt=TaskReceipt(ReceiptKind.FILE, "MOCK-HERDR-RECEIPT.txt"),
                ),
            )

        self.assertFalse(outcome.task_verified)
        self.assertEqual(outcome.error_code, "task_receipt_stale")
        self.assertEqual(outcome.state, AgentState.UNKNOWN)

    def test_verifies_receipt_file_created_during_current_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            receipt_path = workspace / "MOCK-HERDR-RECEIPT.txt"
            get_calls = 0

            def runner(
                argv: list[str],
                *,
                cwd: str,
                timeout: float | None,
            ) -> subprocess.CompletedProcess[str]:
                nonlocal get_calls
                if argv[0:3] == ["herdr", "agent", "get"]:
                    get_calls += 1
                    return _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    )
                if argv[0:3] == ["herdr", "agent", "prompt"]:
                    receipt_path.write_text("MOCK-WORKTREE-OK\n", encoding="utf-8")
                    return _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    )
                raise AssertionError(f"unexpected call: {argv}")

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

            outcome = transport.dispatch(
                Harness.GROK,
                "write receipt",
                timeout_seconds=30,
                context=DispatchContext(
                    placement=PlacementTarget.PANE,
                    title="write receipt",
                    task_key="**********",
                    receipt=TaskReceipt(ReceiptKind.FILE, "MOCK-HERDR-RECEIPT.txt"),
                ),
            )

        self.assertTrue(outcome.task_verified)
        self.assertEqual(outcome.state, AgentState.DONE)

    def test_rejects_symlinked_receipt_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            target = workspace / "actual-receipt.txt"
            target.write_text("fresh\n", encoding="utf-8")
            (workspace / "receipt.txt").symlink_to(target.name)

            with self.assertRaisesRegex(TransportError, "task_receipt_path_invalid"):
                HerdrTransport._receipt_file_path(
                    TaskReceipt(ReceiptKind.FILE, "receipt.txt"),
                    workspace,
                )

    def test_reports_invalid_provider_model_in_settled_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        (
                            "API failed after 5 retries\n"
                            "ValidationException: The provided model identifier is invalid"
                        ),
                        "",
                    ),
                ]
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
            )

            outcome = transport.dispatch(Harness.HERMES, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.error_code, "agent_model_invalid")
        self.assertIs(outcome.agent_settled, True)
        self.assertEqual(
            outcome.error_summary,
            "ValidationException: The provided model identifier is invalid",
        )
        self.assertEqual(outcome.state, AgentState.UNKNOWN)

    def test_reports_provider_retry_exhaustion_in_settled_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "hermes",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "API failed after 5 retries: provider unavailable",
                        "",
                    ),
                ]
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
            )

            outcome = transport.dispatch(Harness.HERMES, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.error_code, "agent_provider_failed")
        self.assertIs(outcome.agent_settled, True)
        self.assertEqual(outcome.state, AgentState.UNKNOWN)

    def test_reports_device_login_wait_in_settled_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "droid",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        (
                            "Factory device login\n"
                            "Visit https://example.invalid/device and enter code ABCD\n"
                            "Waiting for authentication"
                        ),
                        "",
                    ),
                ]
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
            )

            outcome = transport.dispatch(Harness.DROID, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.error_code, "agent_auth_required")
        self.assertEqual(outcome.state, AgentState.UNKNOWN)

    def test_reports_cross_harness_auth_failure_in_settled_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "grok",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "API Error: 401 Unauthorized - invalid API key",
                        "",
                    ),
                ]
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
            )

            outcome = transport.dispatch(Harness.GROK, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.error_code, "agent_auth_failed")
        self.assertEqual(outcome.state, AgentState.UNKNOWN)

    def test_reports_generic_provider_403_in_settled_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "interactive_ready": True,
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "idle",
                                "pane_id": "w1:p9",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "pi",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "Error: 403 status code (no body)",
                        "",
                    ),
                ]
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
            )

            outcome = transport.dispatch(Harness.PI, "inspect", timeout_seconds=30)

        self.assertEqual(outcome.error_code, "agent_auth_failed")
        self.assertEqual(outcome.error_summary, "Error: 403 status code")
        self.assertEqual(outcome.state, AgentState.UNKNOWN)

    def test_responds_to_blocked_agent_and_waits_for_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            blocked = {
                "agent": "codex",
                "agent_status": "blocked",
                "pane_id": "w1:p9",
                "state_change_seq": 5,
            }
            runner = FakeRunner(
                [
                    _result({"agent": blocked}),
                    _result({"type": "ok"}),
                    _result({"type": "ok"}),
                    _result(
                        {
                            "agent": {
                                "agent": "codex",
                                "agent_status": "done",
                                "pane_id": "w1:p9",
                                "state_change_seq": 6,
                            }
                        }
                    ),
                ]
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

            outcome = transport.respond(
                "blocked-worker",
                Harness.CODEX,
                "Approve this local action.",
                timeout_seconds=30,
            )

        self.assertEqual(outcome.state, AgentState.DONE)
        self.assertEqual(outcome.pane_id, "w1:p9")
        self.assertEqual(
            runner.calls[1],
            [
                "herdr",
                "pane",
                "send-text",
                "w1:p9",
                "Approve this local action.",
            ],
        )
        self.assertEqual(
            runner.calls[2],
            ["herdr", "agent", "send-keys", "blocked-worker", "enter"],
        )
        self.assertFalse(any(call[0:3] == ["herdr", "agent", "prompt"] for call in runner.calls))

    def test_blocked_response_respects_an_expired_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner([])
            transport = HerdrTransport(
                "example",
                workspace,
                environ={"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_WORKSPACE_ID": "w1"},
                runner=runner,
            )

            outcome = transport.respond("blocked", Harness.CODEX, "approve", timeout_seconds=0)

        self.assertEqual(outcome.error_code, "herdr_timeout")
        self.assertEqual(runner.calls, [])

    def test_refuses_blocked_response_when_owned_pane_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "name": "blocked-worker",
                                "agent": "codex",
                                "agent_status": "blocked",
                                "workspace_id": "w1",
                                "cwd": str(workspace),
                                "foreground_cwd": str(workspace),
                                "pane_id": "w1:p8",
                                "state_change_seq": 5,
                            }
                        }
                    ),
                ]
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

            outcome = transport.respond(
                "blocked-worker",
                Harness.CODEX,
                "Approve this local action.",
                timeout_seconds=30,
                expected_pane_id="w1:p9",
                context=DispatchContext(
                    placement=PlacementTarget.PANE,
                    title="inspect",
                    task_key="resume-key",
                ),
            )

        self.assertEqual(outcome.error_code, "agent_pane_mismatch")
        self.assertEqual(len(runner.calls), 1)


def _result(result: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["herdr"],
        0,
        json.dumps({"id": "test", "result": result}),
        "",
    )


def _error(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["herdr"],
        1,
        "",
        json.dumps({"error": {"code": code}}),
    )


if __name__ == "__main__":
    unittest.main()
