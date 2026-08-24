from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from herdr_orchestrator.cli import main
from herdr_orchestrator.config import ConfigError, load_workflow
from herdr_orchestrator.runner import Coordinator


class ExecutorConfigTests(unittest.TestCase):
    def test_all_registered_executor_kinds_are_selectable(self) -> None:
        for kind in (
            "research-synthesis",
            "code-review-gate",
            "software-delivery",
            "incident-response",
        ):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as temporary:
                    config = load_workflow(
                        _write_v2_workflow(Path(temporary), executor_kind=kind)
                    )

                    self.assertEqual(config.executor.kind.value, kind)

    def test_loads_schema_v2_with_one_registered_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflow = _write_v2_workflow(Path(temporary), executor_kind="research-synthesis")

            config = load_workflow(workflow)

            self.assertEqual(config.schema_version, 2)
            self.assertEqual(config.executor.kind.value, "research-synthesis")
            self.assertEqual(
                config.runtime_dir,
                (Path(temporary) / ".orchestrator" / "runs" / "example").resolve(),
            )

    def test_v2_requires_executor_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflow = _write_v2_workflow(
                Path(temporary),
                executor_kind="research-synthesis",
                executor_block="",
            )

            with self.assertRaisesRegex(ConfigError, "executor_missing"):
                load_workflow(workflow)

    def test_v2_rejects_unknown_and_multiple_executor_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unknown = _write_v2_workflow(
                Path(temporary),
                executor_kind="not-registered",
            )
            with self.assertRaisesRegex(ConfigError, "unsupported_executor"):
                load_workflow(unknown)

        with tempfile.TemporaryDirectory() as temporary:
            multiple = _write_v2_workflow(
                Path(temporary),
                executor_kind="research-synthesis",
                extra='[executor.research]\nroles = {}\n[executor.review]\naxes = ["security"]',
            )
            with self.assertRaisesRegex(ConfigError, "executor_multiple_configurations"):
                load_workflow(multiple)

    def test_rejects_missing_non_integer_and_unsupported_schema_versions(self) -> None:
        cases = (
            ("name = \"example\"\n", "schema_version_missing"),
            ("schema_version = \"2\"\n", "schema_version_must_be_integer"),
            ("schema_version = 3\n", "unsupported_schema_version: 3"),
        )
        for prefix, expected in cases:
            with self.subTest(prefix=prefix):
                with tempfile.TemporaryDirectory() as temporary:
                    workflow = Path(temporary) / "workflow.toml"
                    workflow.write_text(prefix, encoding="utf-8")

                    with self.assertRaisesRegex(ConfigError, expected):
                        load_workflow(workflow)

    def test_v2_rejects_unknown_and_cross_executor_configuration(self) -> None:
        cases = (
            ("graph = []", "configuration_rejected: graph"),
            ("[review]\naxes = [\"security\"]", "executor_mismatch"),
            ("[research]\nunknown = true", "executor_unknown_field: unknown"),
        )
        for fragment, expected in cases:
            with self.subTest(fragment=fragment):
                with tempfile.TemporaryDirectory() as temporary:
                    workflow = _write_v2_workflow(
                        Path(temporary),
                        executor_kind="research-synthesis",
                        extra=fragment,
                    )

                    with self.assertRaisesRegex(ConfigError, expected):
                        load_workflow(workflow)

    def test_executor_roles_must_reference_declared_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflow = _write_v2_workflow(
                Path(temporary),
                executor_kind="research-synthesis",
                extra='[research.roles]\ncollector = "missing-worker"',
            )

            with self.assertRaisesRegex(ConfigError, "executor_role_worker_not_found"):
                load_workflow(workflow)

    def test_v2_rejects_programmable_execution_fields(self) -> None:
        for field in ("nodes", "stages", "edges", "conditions", "expression", "callback", "shell"):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as temporary:
                    workflow = _write_v2_workflow(
                        Path(temporary),
                        executor_kind="research-synthesis",
                        extra=f"{field} = []",
                    )

                    with self.assertRaisesRegex(ConfigError, f"configuration_rejected: {field}"):
                        load_workflow(workflow)

    def test_declared_static_argv_checks_are_data_not_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflow = _write_v2_workflow(
                Path(temporary),
                executor_kind="code-review-gate",
                extra=(
                    "[review]\n"
                    'checks = [{id = "compile", argv = ["python3", "-m", "compileall"], '
                    'timeout_seconds = 30}]\n'
                ),
            )

            config = load_workflow(workflow)

            self.assertEqual(
                config.executor.settings["checks"][0]["argv"],
                ["python3", "-m", "compileall"],
            )

    def test_v1_rejects_v2_only_fields_before_legacy_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / "workflow.toml"
            workflow.write_text(
                _v1_prefix(root)
                + '\nruntime_dir = ".orchestrator/runs/example"\n'
                + _v1_tables(root),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigError,
                "schema_mismatch: v1_workflow_contains_v2_field",
            ):
                load_workflow(workflow)

    def test_legacy_commands_reject_v2_before_creating_queue_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _write_v2_workflow(root, executor_kind="incident-response")

            for command in ("seed", "status", "smoke"):
                with self.subTest(command=command):
                    error = StringIO()
                    output = StringIO()
                    with redirect_stderr(error), redirect_stdout(output):
                        exit_code = main([command, "--workflow", str(workflow)])
                    if command == "status":
                        self.assertEqual(exit_code, 0)
                        self.assertEqual(error.getvalue(), "")
                        payload = json.loads(output.getvalue())
                        self.assertEqual(payload["schema_version"], 2)
                        self.assertEqual(payload["executor"], "incident-response")
                    else:
                        self.assertEqual(exit_code, 2)
                        self.assertIn(
                            f"schema_mismatch: {command}_requires_schema_v1",
                            error.getvalue(),
                        )

            self.assertTrue((root / ".orchestrator" / "state.db").exists())

    def test_v1_run_rejects_v2_fixture_options_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / "workflow.toml"
            workflow.write_text(
                _v1_prefix(root) + "\n" + _v1_tables(root),
                encoding="utf-8",
            )
            error = StringIO()

            with redirect_stderr(error):
                exit_code = main(
                    [
                        "run",
                        "--workflow",
                        str(workflow),
                        "--once",
                        "--dispatcher-fixture",
                        "valid",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("schema_mismatch: run_options_require_schema_v2", error.getvalue())
            self.assertFalse((root / ".orchestrator").exists())

    def test_legacy_coordinator_rejects_v2_without_initializing_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_workflow(
                _write_v2_workflow(root, executor_kind="research-synthesis")
            )

            with self.assertRaisesRegex(
                ValueError,
                "schema_mismatch: legacy_coordinator_requires_schema_v1",
            ):
                Coordinator(config)

            self.assertFalse((root / ".orchestrator" / "state.db").exists())


def _write_v2_workflow(
    root: Path,
    *,
    executor_kind: str,
    executor_block: str | None = None,
    extra: str = "",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    block = (
        executor_block
        if executor_block is not None
        else f'[executor]\nkind = "{executor_kind}"'
    )
    workflow = root / "workflow.toml"
    workflow.write_text(
        _v2_prefix(root, executor_block=block, extra=extra),
        encoding="utf-8",
    )
    return workflow


def _v2_prefix(root: Path, *, executor_block: str, extra: str = "") -> str:
    return f"""
schema_version = 2
name = "example"
workspace = "."
state_db = ".orchestrator/state.db"
runtime_dir = ".orchestrator/runs/example"

[coordinator]
poll_seconds = 1
max_parallel = 1
lease_seconds = 100
max_attempts = 2
agent_timeout_seconds = 10

{executor_block}

{extra}

[[workers]]
name = "collector"
harness = "droid"
capabilities = ["research.collect"]
replicas = 1
""".strip()


def _v1_prefix(root: Path) -> str:
    prompt = root / "prompt.md"
    prompt.write_text("Read only.", encoding="utf-8")
    return f"""
schema_version = 1
name = "example"
workspace = "."
state_db = ".orchestrator/state.db"
""".strip()


def _v1_tables(root: Path) -> str:
    return """
[coordinator]
poll_seconds = 1
max_parallel = 1
lease_seconds = 100
max_attempts = 2
agent_timeout_seconds = 10

[planner]
enabled = false
harness = "droid"
interval_seconds = 60
prompt_file = "prompt.md"
output_file = ".orchestrator/planner.json"
max_tasks = 10

[[workers]]
name = "worker"
harness = "droid"
capabilities = []
""".strip()


if __name__ == "__main__":
    unittest.main()
