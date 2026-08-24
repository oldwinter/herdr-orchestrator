from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.herdr import HerdrTransport, replica_slot_names, stable_agent_name
from herdr_orchestrator.model import AgentState, Harness
from herdr_orchestrator.protocol import TransportError


class FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: int | None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if not self.responses:
            raise AssertionError(f"unexpected call: {argv}")
        return self.responses.pop(0)


class HerdrTransportTests(unittest.TestCase):
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
        self.assertEqual(runner.calls[6][0:4], ["herdr", "agent", "prompt", outcome.agent_name])
        self.assertIn("--wait", runner.calls[6])
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
            call
            for call in runner.calls[:prompt_call]
            if call[0:3] == ["herdr", "agent", "get"]
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
        self.assertIn(
            ["herdr", "agent", "wait", outcome.agent_name, "--timeout", "120000"],
            runner.calls,
        )

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
        self.assertIn(
            ["herdr", "agent", "wait", outcome.agent_name, "--timeout", "120000"],
            runner.calls,
        )

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
        self.assertFalse(
            any(call[0:3] == ["herdr", "tab", "close"] for call in runner.calls)
        )

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

    def test_prompt_acceptance_returns_working_then_polls_to_done(self) -> None:
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
        self.assertIn("working", prompt_call)
        self.assertIn("blocked", prompt_call)
        self.assertIn("done", prompt_call)
        self.assertIn("idle", prompt_call)
        self.assertEqual(prompt_call[prompt_call.index("--timeout") + 1], "5000")

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
        self.assertFalse(
            any(call[0:3] == ["herdr", "agent", "prompt"] for call in runner.calls)
        )

    def test_stable_name_is_deterministic(self) -> None:
        workspace = Path("/tmp/project")
        self.assertEqual(
            stable_agent_name("example", workspace, Harness.HERMES),
            stable_agent_name("example", workspace, Harness.HERMES),
        )
        self.assertRegex(
            stable_agent_name("example", workspace, Harness.HERMES),
            r"^ho-hermes-[a-f0-9]{8}$",
        )

    def test_replica_slot_names_keep_single_stable_name(self) -> None:
        workspace = Path("/tmp/project")
        self.assertEqual(
            replica_slot_names("example", workspace, Harness.GROK, 1),
            (stable_agent_name("example", workspace, Harness.GROK),),
        )
        names = replica_slot_names("example", workspace, Harness.GROK, 10)
        self.assertEqual(len(names), 10)
        self.assertEqual(len(set(names)), 10)
        self.assertTrue(all(len(name) <= 32 for name in names))
        self.assertRegex(names[0], r"^ho-grok-01-[a-f0-9]{6}$")
        self.assertRegex(names[9], r"^ho-grok-10-[a-f0-9]{6}$")


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
