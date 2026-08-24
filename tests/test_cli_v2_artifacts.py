from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from herdr_orchestrator.cli import main


class V2ArtifactCliTests(unittest.TestCase):
    def test_status_and_inspect_expose_durable_artifact_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))

            status_exit, status = _invoke(
                "status",
                "--workflow",
                str(workflow),
            )
            self.assertEqual(status_exit, 0)
            self.assertEqual(status["schema_version"], 2)
            self.assertEqual(status["runs"], [])

            start_exit, started = _invoke(
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "cli-run",
                "--input",
                "question",
            )
            self.assertEqual(start_exit, 0)
            run_id = started["run_id"]

            fixture_exit, fixture = _invoke(
                "artifact-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "valid",
            )
            self.assertEqual(fixture_exit, 0)
            self.assertEqual(fixture["code"], "artifact_admitted")
            fixture_run_id = fixture["run_id"]
            after_fixture_exit, after_fixture_status = _invoke(
                "status",
                "--workflow",
                str(workflow),
            )
            self.assertEqual(after_fixture_exit, 0)
            self.assertEqual(len(after_fixture_status["runs"]), 1)

            inspect_exit, inspected = _invoke(
                "inspect",
                "--workflow",
                str(workflow),
                "--run-id",
                run_id,
            )
            self.assertEqual(inspect_exit, 0)
            self.assertEqual(inspected["run_id"], run_id)
            self.assertEqual(inspected["schema_version"], 2)
            for key in (
                "manifest",
                "work_items",
                "dependencies",
                "attempts",
                "artifacts",
                "receipts",
                "events",
            ):
                self.assertIn(key, inspected)
            self.assertEqual(inspected["artifacts"], [])
            self.assertEqual(inspected["receipts"], [])
            fixture_inspect = fixture["inspect"]
            self.assertEqual(
                [item["state"] for item in fixture_inspect["artifacts"]],
                ["admitted"],
            )
            self.assertEqual(len(fixture_inspect["receipts"]), 1)

            repeated_exit, repeated = _invoke(
                "inspect",
                "--workflow",
                str(workflow),
                "--run-id",
                run_id,
            )
            self.assertEqual(repeated_exit, 0)
            for key in (
                "run_id",
                "state",
                "manifest",
                "work_items",
                "artifacts",
                "receipts",
                "events",
            ):
                self.assertEqual(repeated[key], inspected[key])
            self.assertNotEqual(run_id, fixture_run_id)

    def test_unknown_and_ambiguous_inspect_selectors_are_structured_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            _invoke(
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "one",
            )
            _invoke(
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "two",
            )

            unknown_exit, unknown = _invoke(
                "inspect",
                "--workflow",
                str(workflow),
                "--run-id",
                "run_missing",
            )
            self.assertEqual(unknown_exit, 64)
            self.assertEqual(unknown["code"], "run_not_found")

            ambiguous_exit, ambiguous = _invoke(
                "inspect",
                "--workflow",
                str(workflow),
            )
            self.assertEqual(ambiguous_exit, 64)
            self.assertEqual(ambiguous["code"], "run_selector_ambiguous")


def _invoke(*argv: str) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(list(argv))
    payload = json.loads(output.getvalue())
    if not isinstance(payload, dict):
        raise AssertionError(payload)
    return exit_code, payload


def _workflow(root: Path) -> Path:
    workflow = root / "workflow.toml"
    workflow.write_text(
        """
schema_version = 2
name = "cli-v2"
workspace = "."
state_db = ".orchestrator/state.db"
runtime_dir = ".orchestrator/runs/cli-v2"

[coordinator]
poll_seconds = 1
max_parallel = 1
lease_seconds = 120
max_attempts = 2
agent_timeout_seconds = 10

[executor]
kind = "research-synthesis"

[[workers]]
name = "collector"
harness = "droid"
capabilities = []
replicas = 1
""".strip(),
        encoding="utf-8",
    )
    return workflow


if __name__ == "__main__":
    unittest.main()
