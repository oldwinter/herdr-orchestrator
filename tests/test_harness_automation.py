from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.herdr import MAXIMUM_AUTOMATION_ARGUMENTS, HerdrTransport
from herdr_orchestrator.model import Harness


class FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self.responses = responses
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


class HarnessAutomationTests(unittest.TestCase):
    def test_starts_every_harness_with_maximum_automation_arguments(self) -> None:
        expected = {
            Harness.DROID: ("--auto", "high"),
            Harness.GROK: (
                "--always-approve",
                "--permission-mode",
                "bypassPermissions",
            ),
            Harness.CODEX: (
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
            ),
            Harness.PI: ("--approve",),
            Harness.CLAUDE: ("--dangerously-skip-permissions",),
            Harness.HERMES: ("--yolo", "--accept-hooks"),
        }
        self.assertEqual(MAXIMUM_AUTOMATION_ARGUMENTS, expected)

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for harness, native_arguments in expected.items():
                runner = FakeRunner([_result({"agent": {}})])
                transport = _transport(workspace, runner)

                transport._start_agent(
                    f"test-{harness.value}",
                    harness,
                    "w1:p2",
                    workspace,
                )

                self.assertEqual(
                    runner.calls,
                    [
                        [
                            "herdr",
                            "agent",
                            "start",
                            f"test-{harness.value}",
                            "--kind",
                            harness.value,
                            "--pane",
                            "w1:p2",
                            "--timeout",
                            "120000",
                            "--",
                            *native_arguments,
                        ]
                    ],
                )

    def test_accepts_only_the_claude_workspace_trust_startup_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _error("agent_not_ready"),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        (
                            "Accessing workspace:\n"
                            f"{workspace.resolve()}\n"
                            "Quick safety check:\n"
                            "1. Yes, I trust this folder\n"
                            "2. No, exit\n"
                        ),
                        "",
                    ),
                    _result({"type": "ok"}),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "name": "test-claude",
                                "pane_id": "w1:p2",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                ]
            )

            result = _transport(workspace, runner)._start_agent(
                "test-claude",
                Harness.CLAUDE,
                "w1:p2",
                workspace,
            )

        self.assertEqual(result["agent"]["agent_status"], "idle")
        self.assertEqual(
            runner.calls[1],
            [
                "herdr",
                "agent",
                "read",
                "test-claude",
                "--source",
                "detection",
                "--lines",
                "160",
            ],
        )
        self.assertEqual(
            runner.calls[2],
            ["herdr", "agent", "send-keys", "test-claude", "enter"],
        )

    def test_accepts_claude_trust_when_start_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "blocked",
                                "name": "test-claude",
                                "pane_id": "w1:p2",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        (
                            "Accessing workspace:\n"
                            f"{workspace.resolve()}\n"
                            "Quick safety check:\n"
                            "1. Yes, I trust this folder\n"
                            "2. No, exit\n"
                        ),
                        "",
                    ),
                    _result({"type": "ok"}),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "idle",
                                "interactive_ready": True,
                                "name": "test-claude",
                                "pane_id": "w1:p2",
                                "state_change_seq": 2,
                            }
                        }
                    ),
                ]
            )

            result = _transport(workspace, runner)._start_agent(
                "test-claude",
                Harness.CLAUDE,
                "w1:p2",
                workspace,
            )

        self.assertEqual(result["agent"]["agent_status"], "idle")
        self.assertEqual(
            runner.calls[1],
            [
                "herdr",
                "agent",
                "read",
                "test-claude",
                "--source",
                "detection",
                "--lines",
                "160",
            ],
        )
        self.assertEqual(
            runner.calls[2],
            ["herdr", "agent", "send-keys", "test-claude", "enter"],
        )
        self.assertEqual(
            runner.calls[3][:4],
            ["herdr", "agent", "wait", "test-claude"],
        )

    def test_does_not_answer_a_different_claude_startup_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _error("agent_not_ready"),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        "Authentication required. Please run /login.\n",
                        "",
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "blocked",
                                "name": "test-claude",
                                "pane_id": "w1:p2",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                ]
            )

            result = _transport(workspace, runner)._start_agent(
                "test-claude",
                Harness.CLAUDE,
                "w1:p2",
                workspace,
            )

        self.assertEqual(result["agent"]["agent_status"], "blocked")
        self.assertFalse(any(call[0:3] == ["herdr", "agent", "send-keys"] for call in runner.calls))

    def test_does_not_trust_a_different_claude_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _error("agent_not_ready"),
                    subprocess.CompletedProcess(
                        ["herdr"],
                        0,
                        (
                            "Accessing workspace:\n"
                            "/tmp/different-workspace\n"
                            "Quick safety check:\n"
                            "1. Yes, I trust this folder\n"
                            "2. No, exit\n"
                        ),
                        "",
                    ),
                    _result(
                        {
                            "agent": {
                                "agent": "claude",
                                "agent_status": "blocked",
                                "name": "test-claude",
                                "pane_id": "w1:p2",
                                "state_change_seq": 1,
                            }
                        }
                    ),
                ]
            )

            result = _transport(workspace, runner)._start_agent(
                "test-claude",
                Harness.CLAUDE,
                "w1:p2",
                workspace,
            )

        self.assertEqual(result["agent"]["agent_status"], "blocked")
        self.assertFalse(any(call[0:3] == ["herdr", "agent", "send-keys"] for call in runner.calls))


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
    )


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
