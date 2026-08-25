from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.config import ConfigError, load_workflow
from herdr_orchestrator.model import (
    Harness,
    PlacementMode,
    PlacementTarget,
    TrackerBackend,
    WayfinderMode,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_loads_example_workflow(self) -> None:
        config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")

        self.assertEqual(config.name, "multi-harness")
        self.assertEqual(config.workspace, REPO_ROOT)
        self.assertEqual(config.state_db, REPO_ROOT / ".orchestrator/state.db")
        self.assertEqual(config.placement.mode, PlacementMode.HYBRID)
        self.assertEqual(
            config.placement.worktree_root,
            REPO_ROOT / ".orchestrator/worktrees",
        )
        self.assertEqual(config.profiles_dir, REPO_ROOT / "profiles/harnesses")
        self.assertEqual({profile.harness for profile in config.profiles}, set(Harness))
        self.assertEqual(
            {worker.harness for worker in config.workers},
            set(Harness),
        )
        self.assertFalse(config.planner.enabled)
        self.assertIsNone(config.planner.harness)
        self.assertEqual(config.planner.worker_harnesses, tuple(Harness))
        self.assertEqual(len(config.seed_jobs), 6)
        self.assertEqual(
            config.standardized_delivery.tracker_backend,
            TrackerBackend.LOCAL_MARKDOWN,
        )
        self.assertEqual(
            config.standardized_delivery.tracker_root,
            REPO_ROOT / ".scratch/standardized-delivery",
        )
        self.assertEqual(
            config.standardized_delivery.artifact_root,
            REPO_ROOT / ".orchestrator/deliveries",
        )
        self.assertEqual(config.standardized_delivery.wayfinder, WayfinderMode.AUTO)
        self.assertEqual(config.standardized_delivery.max_parallel, 3)
        self.assertEqual(config.standardized_delivery.review_repair_rounds, 2)

    def test_loads_grok_only_research_workflow(self) -> None:
        config = load_workflow(REPO_ROOT / "workflows/grok-research.toml")

        self.assertEqual(config.name, "grok-research")
        self.assertTrue(config.planner.enabled)
        self.assertEqual(config.planner.harness, Harness.GROK)
        self.assertEqual([worker.harness for worker in config.workers], [Harness.GROK])
        self.assertEqual({job.harness for job in config.seed_jobs}, {Harness.GROK})
        self.assertEqual(config.coordinator.max_parallel, 10)
        self.assertEqual(config.workers[0].replicas, 10)
        self.assertEqual(config.workers[0].placement, PlacementTarget.PANE)

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

    def test_defaults_missing_planner_harness_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow(planner_harness=None),
                encoding="utf-8",
            )

            config = load_workflow(workflow)

        self.assertIsNone(config.planner.harness)
        self.assertEqual(config.planner.worker_harnesses, ())

    def test_rejects_planner_worker_without_configured_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow(planner_worker_harnesses=["grok"]),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "has_no_worker"):
                load_workflow(workflow)

    def test_allows_explicit_controller_outside_worker_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow(planner_harness="grok"),
                encoding="utf-8",
            )

            config = load_workflow(workflow)

        self.assertEqual(config.planner.harness, Harness.GROK)
        self.assertEqual(
            tuple(worker.harness for worker in config.workers),
            (Harness.DROID,),
        )

    def test_loads_configurable_github_delivery_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow() + """
[standardized_delivery]
tracker_backend = "github"
github_repository = "owner/project"
wayfinder = "never"
max_parallel = 2
review_repair_rounds = 1
""",
                encoding="utf-8",
            )

            config = load_workflow(workflow)

        self.assertEqual(
            config.standardized_delivery.tracker_backend,
            TrackerBackend.GITHUB,
        )
        self.assertEqual(config.standardized_delivery.github_repository, "owner/project")
        self.assertEqual(config.standardized_delivery.wayfinder, WayfinderMode.NEVER)
        self.assertEqual(config.standardized_delivery.max_parallel, 2)
        self.assertEqual(config.standardized_delivery.review_repair_rounds, 1)


def _minimal_workflow(
    *,
    worker_harness: str = "droid",
    planner_output: str = ".orchestrator/planner.json",
    planner_harness: str | None = "droid",
    planner_worker_harnesses: list[str] | None = None,
) -> str:
    planner_harness_line = "" if planner_harness is None else f'harness = "{planner_harness}"'
    planner_workers_line = (
        ""
        if planner_worker_harnesses is None
        else f"worker_harnesses = {planner_worker_harnesses!r}".replace("'", '"')
    )
    return f"""
schema_version = 1
name = "example"
workspace = "."
state_db = ".orchestrator/state.db"
profiles_dir = "{REPO_ROOT / "profiles/harnesses"}"

[coordinator]
poll_seconds = 1
max_parallel = 1
lease_seconds = 100
max_attempts = 2
agent_timeout_seconds = 10

[planner]
enabled = false
{planner_harness_line}
{planner_workers_line}
interval_seconds = 60
prompt_file = "prompt.md"
output_file = "{planner_output}"
max_tasks = 10

[[workers]]
name = "worker"
harness = "{worker_harness}"
"""


if __name__ == "__main__":
    unittest.main()
