from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import herdr_orchestrator.cli as cli_module
from herdr_orchestrator.cli import (
    build_parser,
    doctor,
    probe_harness_readiness,
    readiness_matrix,
    smoke,
)
from herdr_orchestrator.completion import CompletionPolicy
from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.delivery import DeliveryEscalation
from herdr_orchestrator.model import (
    AgentState,
    DispatchContext,
    DispatchOutcome,
    Harness,
    ReceiptKind,
)
from herdr_orchestrator.readiness import BuildIdentity, ReadinessEnvironment

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
        droid = next(check for check in report["checks"] if check["check"] == "readiness:droid")
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(droid["status"], "auth_required")
        self.assertFalse(droid["ok"])

    def test_doctor_can_filter_harnesses_and_reports_probe_timing(self) -> None:
        config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        output = io.StringIO()
        probed: list[Harness] = []

        def readiness_probe(
            workflow: object,
            harness: Harness,
            timeout_seconds: int,
        ) -> dict[str, object]:
            probed.append(harness)
            return {
                "status": "ready",
                "error_code": None,
                "error_summary": None,
                "duration_ms": 7,
            }

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
                selected_harnesses=["droid"],
            )

        report = json.loads(output.getvalue())
        readiness = [check for check in report["checks"] if check["check"].startswith("readiness:")]
        self.assertEqual(code, 0)
        self.assertEqual(probed, [Harness.DROID])
        self.assertEqual([check["check"] for check in readiness], ["readiness:droid"])
        self.assertEqual(readiness[0]["duration_ms"], 7)
        self.assertEqual(report["summary"]["harnesses"], ["droid"])
        self.assertEqual(report["summary"]["readiness_ms"], 7)

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

    def test_doctor_accepts_repeatable_harness_filter(self) -> None:
        args = build_parser().parse_args(
            [
                "doctor",
                "--workflow",
                "workflow.toml",
                "--harness",
                "droid",
                "--harness",
                "codex",
            ]
        )

        self.assertEqual(args.harness, ["droid", "codex"])

    def test_readiness_matrix_accepts_repeatable_harness_filter(self) -> None:
        args = build_parser().parse_args(
            [
                "readiness-matrix",
                "--workflow",
                "workflow.toml",
                "--harness",
                "droid",
                "--harness",
                "codex",
            ]
        )

        self.assertEqual(args.harness, ["droid", "codex"])

    def test_readiness_matrix_prints_structured_current_build_evidence(self) -> None:
        config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        output = io.StringIO()
        environment = ReadinessEnvironment(
            True,
            {harness: True for harness in Harness},
            {harness: True for harness in Harness},
        )

        with redirect_stdout(output):
            code = readiness_matrix(
                config,
                selected_harnesses=["droid"],
                probe_timeout_seconds=15,
                environment=environment,
                build=BuildIdentity("a" * 40, "0.1.6"),
                readiness_probe=lambda *args: {
                    "status": "ready",
                    "error_code": None,
                    "phase_timings_ms": {"total": 7},
                },
                clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
            )

        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["verification"], "VERIFIED")
        self.assertEqual(report["results"][0]["harness"], "droid")
        self.assertEqual(report["results"][0]["attempt_count"], 1)
        self.assertEqual(report["commit"], "a" * 40)

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

    def test_enqueue_accepts_structured_completion_policy(self) -> None:
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
                "inspect-v2",
                "--completion-policy",
                "structured-v2",
            ]
        )

        self.assertEqual(args.completion_policy, CompletionPolicy.STRUCTURED_V2.value)

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

    def test_resume_accepts_job_and_response_file(self) -> None:
        args = build_parser().parse_args(
            [
                "resume",
                "--workflow",
                "workflow.toml",
                "--job-id",
                "42",
                "--response-file",
                "approval.txt",
            ]
        )

        self.assertEqual(args.job_id, 42)
        self.assertEqual(args.response_file, "approval.txt")

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
        self.assertFalse(args.failed_agents)
        self.assertFalse(args.apply)

    def test_gc_accepts_owned_failed_agents_scope(self) -> None:
        args = build_parser().parse_args(
            [
                "gc",
                "--workflow",
                "workflow.toml",
                "--failed-agents",
            ]
        )

        self.assertFalse(args.succeeded_agents)
        self.assertTrue(args.failed_agents)
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
            workflow_path = workspace / ".herdr-orchestrator/workflows/multi-harness.toml"
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

    def test_smoke_rejects_harness_without_enabled_worker(self) -> None:
        config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        config = replace(
            config,
            workers=tuple(worker for worker in config.workers if worker.harness is Harness.CODEX),
        )

        with (
            patch("herdr_orchestrator.cli.HerdrTransport") as transport,
            self.assertRaisesRegex(ValueError, "smoke_harness_not_enabled: pi"),
        ):
            smoke(config, selected_harnesses=["pi"])

        transport.assert_not_called()

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


class CliCommandDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")

    def test_simple_coordinator_and_store_commands_emit_json(self) -> None:
        args = Namespace(apply=False, extra_attempts=2, job_id=7)
        coordinator = MagicMock()
        coordinator.seed.return_value = (2, 1)
        coordinator.gc_succeeded_agents.return_value = {"candidate_count": 0}
        store = MagicMock()
        store.retry_failed.return_value = {"job_id": 7, "state": "pending"}
        store.status_counts.return_value = {"pending": 0}
        store.jobs.return_value = []
        output = io.StringIO()
        with (
            patch.object(cli_module, "Coordinator", return_value=coordinator),
            patch.object(cli_module, "Store", return_value=store),
            redirect_stdout(output),
        ):
            self.assertEqual(cli_module._command_seed(self.config, args), 0)
            self.assertEqual(cli_module._command_gc(self.config, args), 0)
            self.assertEqual(cli_module._command_retry(self.config, args), 0)
            self.assertEqual(cli_module._command_status(self.config, args), 0)
        self.assertIn('"added": 2', output.getvalue())
        store.initialize.assert_called()

    def test_enqueue_and_run_modes_forward_typed_arguments(self) -> None:
        coordinator = MagicMock()
        coordinator.enqueue_prompt_file.return_value = (9, True, Harness.PI)
        coordinator.run_once.return_value = {"claimed": 1}
        coordinator.run_until_idle.return_value = {"idle": False}
        args = Namespace(
            controller_harness=None,
            dedupe_key="task-v1",
            drain_timeout_seconds=60,
            harness="pi",
            once=False,
            placement="tab",
            prompt_file="task.md",
            receipt_file=None,
            receipt_prefix=None,
            title="Task",
            until_idle=False,
            worker_harness=None,
        )
        with (
            patch.object(cli_module, "_coordinator_from_args", return_value=coordinator),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli_module._command_enqueue(self.config, args), 0)
            args.once = True
            self.assertEqual(cli_module._command_run(self.config, args), 0)
            args.once = False
            args.until_idle = True
            self.assertEqual(cli_module._command_run(self.config, args), 1)
            args.until_idle = False
            coordinator.run_forever.side_effect = KeyboardInterrupt
            self.assertEqual(cli_module._command_run(self.config, args), 0)
        coordinator.enqueue_prompt_file.assert_called_once()

    def test_run_rejects_unbounded_drain(self) -> None:
        args = Namespace(
            controller_harness=None,
            drain_timeout_seconds=0,
            once=False,
            until_idle=True,
            worker_harness=None,
        )
        with (
            patch.object(cli_module, "_coordinator_from_args", return_value=MagicMock()),
            self.assertRaisesRegex(ValueError, "drain_timeout_out_of_range"),
        ):
            cli_module._command_run(self.config, args)

    def test_delivery_overrides_and_command_result(self) -> None:
        args = Namespace(
            controller_harness="grok",
            github_repository="oldwinter/herdr-orchestrator",
            goal_file="goal.md",
            max_parallel=2,
            review_repair_rounds=1,
            tracker_backend="github",
            tracker_root=".scratch/tracker",
            wayfinder="always",
            worker_harness=["codex"],
        )
        delivery = MagicMock()
        delivery.run.return_value = SimpleNamespace(
            artifact_root=Path(".orchestrator/deliveries/run"),
            integration_branch="delivery/run",
            integration_commit="abc123",
            review_rounds=1,
            run_id="run",
            status="completed",
            tickets_completed=2,
            tracker_references={"01": "#1"},
        )
        configured = cli_module._delivery_config(self.config, args)
        self.assertEqual(configured.standardized_delivery.max_parallel, 2)
        with (
            patch.object(cli_module, "StandardizedDelivery", return_value=delivery),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli_module._command_deliver(self.config, args), 0)

    def test_catalog_profile_doctor_and_dashboard_commands(self) -> None:
        dashboard = MagicMock()
        dashboard.address = ("127.0.0.1", 8765)
        dashboard.serve_forever.side_effect = KeyboardInterrupt
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                cli_module._command_catalog(self.config, Namespace(format="json")),
                0,
            )
            self.assertEqual(
                cli_module._command_catalog(self.config, Namespace(format="text")),
                0,
            )
            self.assertEqual(
                cli_module._command_profile(
                    self.config,
                    Namespace(format="json", harness="pi"),
                ),
                0,
            )
            self.assertEqual(
                cli_module._command_profile(
                    self.config,
                    Namespace(format="text", harness="pi"),
                ),
                0,
            )
            with patch.object(cli_module, "doctor", return_value=0):
                self.assertEqual(
                    cli_module._command_doctor(
                        self.config,
                        Namespace(probe_timeout_seconds=30),
                    ),
                    0,
                )
            with patch.object(cli_module, "DashboardServer", return_value=dashboard):
                self.assertEqual(
                    cli_module._command_dashboard(
                        self.config,
                        Namespace(host="127.0.0.1", poll_seconds=1.0, port=0),
                    ),
                    0,
                )
        self.assertIn('"harnesses"', output.getvalue())

    def test_main_dispatches_and_classifies_errors(self) -> None:
        handler = MagicMock(return_value=7)
        with (
            patch.object(cli_module, "load_workflow", return_value=self.config),
            patch.dict(cli_module.COMMAND_HANDLERS, {"catalog": handler}),
        ):
            code = cli_module.main(["catalog", "--workflow", "workflow.toml", "--format", "json"])
        self.assertEqual(code, 7)
        with (
            patch.object(cli_module, "load_workflow", side_effect=ValueError("invalid")),
            patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(
                cli_module.main(["catalog", "--workflow", "workflow.toml"]),
                2,
            )
        handler.side_effect = DeliveryEscalation("account_required")
        with (
            patch.object(cli_module, "load_workflow", return_value=self.config),
            patch.dict(cli_module.COMMAND_HANDLERS, {"catalog": handler}),
            patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(
                cli_module.main(["catalog", "--workflow", "workflow.toml"]),
                3,
            )


if __name__ == "__main__":
    unittest.main()
