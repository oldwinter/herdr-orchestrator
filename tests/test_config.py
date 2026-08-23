from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.config import ConfigError, load_workflow
from herdr_orchestrator.model import Harness

REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_loads_example_workflow(self) -> None:
        config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")

        self.assertEqual(config.name, "multi-harness")
        self.assertEqual(config.workspace, REPO_ROOT)
        self.assertEqual(config.state_db, REPO_ROOT / ".orchestrator/state.db")
        self.assertEqual(
            {worker.harness for worker in config.workers},
            set(Harness),
        )
        self.assertFalse(config.planner.enabled)
        self.assertEqual(len(config.seed_jobs), 5)

    def test_rejects_unknown_harness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow(worker_harness="unknown"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "unsupported_harness"):
                load_workflow(workflow)

    def test_requires_planner_output_in_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow(planner_output="planner.json"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "planner_output_must_be"):
                load_workflow(workflow)

    def test_requires_lease_to_cover_agent_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow().replace(
                    "lease_seconds = 100",
                    "lease_seconds = 99",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "lease_seconds_must_cover"):
                load_workflow(workflow)


def _minimal_workflow(
    *,
    worker_harness: str = "droid",
    planner_output: str = ".orchestrator/planner.json",
) -> str:
    return f"""
schema_version = 1
name = "example"
workspace = "."
state_db = ".orchestrator/state.db"

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
output_file = "{planner_output}"
max_tasks = 10

[[workers]]
name = "worker"
harness = "{worker_harness}"
capabilities = []
"""


if __name__ == "__main__":
    unittest.main()
