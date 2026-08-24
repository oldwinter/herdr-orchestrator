from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.cli import build_parser, doctor, probe_harness_readiness, smoke
from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.model import (
    AgentState,
    DispatchContext,
    DispatchOutcome,
    Harness,
    ReceiptKind,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_readiness_probe_classifies_invalid_model_and_closes_created_agent(self) -> None:
        config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")

        class FakeTransport:
            def __init__(self) -> None:
                self.closed: list[str] = []
                self.dispatched: list[str] = []

            def dispatch(self, *args: object, **kwargs: object) -> DispatchOutcome:
                self.dispatched.append(str(kwargs["agent_name"]))
                return DispatchOutcome(
                    self.dispatched[-1],
                    AgentState.UNKNOWN,
                    False,
                    "w1:p2",
                    "agent_model_invalid",
                    error_summary="ValidationException: model identifier invalid",
                )

            def close_created_agent(self, name: str) -> None:
                self.closed.append(name)

        transport = FakeTransport()

        result = probe_harness_readiness(
            config,
            Harness.HERMES,
            15,
            transport=transport,
        )

        self.assertEqual(result["status"], "model_invalid")
        self.assertEqual(transport.closed, transport.dispatched)
        self.assertRegex(transport.closed[0], r"^doctor-hermes-[a-f0-9]{6}$")

    def test_doctor_fails_when_an_installed_harness_requires_auth(self) -> None:
        config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        output = io.StringIO()

        def readiness_probe(
            workflow: object,
            harness: Harness,
            timeout_seconds: int,
        ) -> dict[str, object]:
            if harness is Harness.DROID:
                return {
                    "status": "auth_required",
                    "error_code": "agent_auth_required",
                    "error_summary": "Factory device login",
                }
            return {"status": "ready", "error_code": None, "error_summary": None}

        with redirect_stdout(output):
            code = doctor(
                config,
                environ={
                    "HERDR_ENV": "1",
                    "HERDR_PANE_ID": "w1:p1",
                    "HERDR_WORKSPACE_ID": "w1",
                },
                which=lambda name: f"/bin/{name}",
                version_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                    ["herdr", "--version"],
                    0,
                    "herdr 0.8.2\n",
                    "",
                ),
                readiness_probe=readiness_probe,
                probe_timeout_seconds=15,
            )

        report = json.loads(output.getvalue())
        droid = next(
            check for check in report["checks"] if check["check"] == "readiness:droid"
        )
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(droid["status"], "auth_required")
        self.assertFalse(droid["ok"])

    def test_dashboard_defaults_to_loopback_live_view(self) -> None:
        args = build_parser().parse_args(
            [
                "dashboard",
                "--workflow",
                "workflow.toml",
            ]
        )

        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.poll_seconds, 2.0)

    def test_enqueue_defaults_to_automatic_selection(self) -> None:
        args = build_parser().parse_args(
            [
                "enqueue",
                "--workflow",
                "workflow.toml",
                "--title",
                "Build",
                "--prompt-file",
                "task.md",
                "--dedupe-key",
                "build-v1",
            ]
        )

        self.assertEqual(args.harness, "auto")
        self.assertEqual(args.placement, "auto")
        self.assertIsNone(args.controller_harness)
        self.assertIsNone(args.worker_harness)

    def test_enqueue_accepts_a_declared_output_receipt(self) -> None:
        args = build_parser().parse_args(
            [
                "enqueue",
                "--workflow",
                "workflow.toml",
                "--title",
                "Inspect",
                "--prompt-file",
                "task.md",
                "--dedupe-key",
                "inspect-v1",
                "--receipt-prefix",
                "MOCK-OK harness=pi",
            ]
        )

        self.assertEqual(args.receipt_prefix, "MOCK-OK harness=pi")
        self.assertIsNone(args.receipt_file)

    def test_run_accepts_separate_controller_and_worker_overrides(self) -> None:
        args = build_parser().parse_args(
            [
                "run",
                "--workflow",
                "workflow.toml",
                "--once",
                "--controller-harness",
                "grok",
                "--worker-harness",
                "codex",
                "--worker-harness",
                "pi",
            ]
        )

        self.assertEqual(args.controller_harness, "grok")
        self.assertEqual(args.worker_harness, ["codex", "pi"])

    def test_run_accepts_bounded_until_idle_mode(self) -> None:
        args = build_parser().parse_args(
            [
                "run",
                "--workflow",
                "workflow.toml",
                "--until-idle",
                "--drain-timeout-seconds",
                "120",
            ]
        )

        self.assertTrue(args.until_idle)
        self.assertFalse(args.once)
        self.assertEqual(args.drain_timeout_seconds, 120)

    def test_retry_accepts_job_and_attempt_budget(self) -> None:
        args = build_parser().parse_args(
            [
                "retry",
                "--workflow",
                "workflow.toml",
                "--job-id",
                "42",
                "--extra-attempts",
                "2",
            ]
        )

        self.assertEqual(args.job_id, 42)
        self.assertEqual(args.extra_attempts, 2)

    def test_gc_defaults_to_dry_run_for_succeeded_agents(self) -> None:
        args = build_parser().parse_args(
            [
                "gc",
                "--workflow",
                "workflow.toml",
                "--succeeded-agents",
            ]
        )

        self.assertTrue(args.succeeded_agents)
        self.assertFalse(args.apply)

    def test_smoke_uses_target_files_and_requires_an_output_receipt(self) -> None:
        base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")

        class FakeTransport:
            instances: list[FakeTransport] = []

            def __init__(self, workflow_name: str, workspace: Path) -> None:
                self.calls: list[tuple[str, DispatchContext]] = []
                self.closed: list[str] = []
                self.instances.append(self)

            def dispatch(
                self,
                harness: Harness,
                prompt: str,
                **kwargs: object,
            ) -> DispatchOutcome:
                context = kwargs["context"]
                assert isinstance(context, DispatchContext)
                self.calls.append((prompt, context))
                return DispatchOutcome(
                    str(kwargs["agent_name"]),
                    AgentState.DONE,
                    False,
                    "w1:p2",
                    task_verified=True,
                )

            def close_created_agent(self, name: str) -> None:
                self.closed.append(name)

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            workflow_path = (
                workspace / ".herdr-orchestrator/workflows/multi-harness.toml"
            )
            workflow_path.parent.mkdir(parents=True)
            workflow_path.write_text("schema_version = 1\n", encoding="utf-8")
            (workspace / "README.md").write_text("# Target\n", encoding="utf-8")
            config = replace(base, path=workflow_path, workspace=workspace)
            output = io.StringIO()

            with (
                patch("herdr_orchestrator.cli.HerdrTransport", FakeTransport),
                redirect_stdout(output),
            ):
                code = smoke(config, selected_harnesses=["pi"])

        self.assertEqual(code, 0, output.getvalue())
        transport = FakeTransport.instances[0]
        self.assertEqual(len(transport.calls), 1)
        prompt, context = transport.calls[0]
        self.assertIn("README.md", prompt)
        self.assertIn(".herdr-orchestrator/workflows/multi-harness.toml", prompt)
        self.assertNotIn("pyproject.toml", prompt)
        self.assertIsNotNone(context.receipt)
        assert context.receipt is not None
        self.assertIs(context.receipt.kind, ReceiptKind.OUTPUT_PREFIX)
        self.assertIn(context.receipt.value, prompt)

    def test_deliver_is_explicit_and_accepts_bounded_overrides(self) -> None:
        args = build_parser().parse_args(
            [
                "deliver",
                "--workflow",
                "workflow.toml",
                "--goal-file",
                "goal.md",
                "--tracker-backend",
                "local-markdown",
                "--wayfinder",
                "auto",
                "--max-parallel",
                "3",
                "--review-repair-rounds",
                "2",
                "--controller-harness",
                "grok",
                "--worker-harness",
                "codex",
            ]
        )

        self.assertEqual(args.goal_file, "goal.md")
        self.assertEqual(args.tracker_backend, "local-markdown")
        self.assertEqual(args.wayfinder, "auto")
        self.assertEqual(args.max_parallel, 3)
        self.assertEqual(args.review_repair_rounds, 2)
        self.assertEqual(args.controller_harness, "grok")
        self.assertEqual(args.worker_harness, ["codex"])


if __name__ == "__main__":
    unittest.main()
