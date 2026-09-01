from __future__ import annotations

import shutil
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
        self.assertEqual(config.coordinator.max_parallel, 6)
        self.assertEqual(config.coordinator.lease_seconds, 32400)
        self.assertEqual(config.coordinator.agent_timeout_seconds, 28800)

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
        self.assertEqual(config.coordinator.lease_seconds, 32400)
        self.assertEqual(config.coordinator.agent_timeout_seconds, 28800)

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

    def test_allows_day_long_agent_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow()
                .replace(
                    "lease_seconds = 100",
                    "lease_seconds = 86400",
                )
                .replace(
                    "agent_timeout_seconds = 10",
                    "agent_timeout_seconds = 86310",
                ),
                encoding="utf-8",
            )

            config = load_workflow(workflow)

        self.assertEqual(config.coordinator.lease_seconds, 86400)
        self.assertEqual(config.coordinator.agent_timeout_seconds, 86310)

    def test_rejects_agent_timeout_above_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow().replace(
                    "agent_timeout_seconds = 10",
                    "agent_timeout_seconds = 86401",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigError,
                "agent_timeout_seconds_must_be_integer_10_86400",
            ):
                load_workflow(workflow)

    def test_rejects_planner_prompt_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside_prompt = root.parent / "planner-outside.md"
            (root / "prompt.md").write_text("task", encoding="utf-8")
            outside_prompt.write_text("outside", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow().replace(
                    'prompt_file = "prompt.md"',
                    f'prompt_file = "{outside_prompt.as_posix()}"',
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "planner_prompt_must_be_in_workspace"):
                load_workflow(workflow)

    def test_accepts_absolute_planner_prompt_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "prompt.md"
            prompt.write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow().replace(
                    'prompt_file = "prompt.md"',
                    f'prompt_file = "{prompt.as_posix()}"',
                ),
                encoding="utf-8",
            )

            config = load_workflow(workflow)

        self.assertEqual(config.planner.prompt_file, prompt)

    def test_preserves_external_state_and_tracker_roots_for_trusted_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root.parent / "shared-workflow-state"
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow().replace(
                    'state_db = ".orchestrator/state.db"',
                    f'state_db = "{(outside / "state.db").as_posix()}"',
                )
                + (
                    "\n[standardized_delivery]\n"
                    f'tracker_root = "{(outside / "tracker").as_posix()}"\n'
                ),
                encoding="utf-8",
            )

            config = load_workflow(workflow)

        self.assertEqual(config.state_db, outside / "state.db")
        self.assertEqual(config.standardized_delivery.tracker_root, outside / "tracker")

    def test_constrains_worktree_and_delivery_artifact_roots_to_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")

            worktree_workflow = root / "worktree.toml"
            worktree_workflow.write_text(
                _minimal_workflow() + '\n[placement]\nworktree_root = "../outside-worktrees"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigError,
                "placement_worktree_root_must_be_in_workspace_runtime",
            ):
                load_workflow(worktree_workflow)

            artifact_workflow = root / "artifact.toml"
            artifact_workflow.write_text(
                _minimal_workflow()
                + '\n[standardized_delivery]\nartifact_root = "../outside-artifacts"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigError,
                "delivery_artifact_root_must_be_in_workspace_runtime",
            ):
                load_workflow(artifact_workflow)

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

    def test_trims_optional_configuration_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow().replace(
                    'name = "worker"\nharness = "droid"',
                    'name = "worker"\nharness = "droid"\nplacement = " pane "',
                )
                + """
[placement]
mode = " hybrid "
worktree_root = " .orchestrator/worktrees "

[standardized_delivery]
tracker_backend = " local-markdown "
wayfinder = " auto "
""",
                encoding="utf-8",
            )

            config = load_workflow(workflow)

        self.assertEqual(config.placement.mode, PlacementMode.HYBRID)
        self.assertEqual(config.placement.worktree_root, root / ".orchestrator/worktrees")
        self.assertEqual(config.workers[0].placement, PlacementTarget.PANE)
        self.assertEqual(
            config.standardized_delivery.tracker_backend,
            TrackerBackend.LOCAL_MARKDOWN,
        )
        self.assertEqual(config.standardized_delivery.wayfinder, WayfinderMode.AUTO)

    def test_rejects_invalid_workflow_encoding_as_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / "workflow.toml"
            workflow.write_bytes(b"schema_version = 1\n\xff")

            with self.assertRaisesRegex(ConfigError, "workflow_invalid_encoding"):
                load_workflow(workflow)

    def test_rejects_invalid_path_values_as_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "workflow_path_invalid"):
                load_workflow(Path("bad\x00workflow.toml"))

            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow().replace(
                    'state_db = ".orchestrator/state.db"',
                    'state_db = "bad\\u0000path"',
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "path_invalid"):
                load_workflow(workflow)

    def test_rejects_invalid_profile_encoding_as_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("task", encoding="utf-8")
            profiles = root / "profiles"
            shutil.copytree(REPO_ROOT / "profiles/harnesses", profiles)
            (profiles / "droid.toml").write_bytes(b"\xff")
            workflow = root / "workflow.toml"
            workflow.write_text(
                _minimal_workflow().replace(
                    str(REPO_ROOT / "profiles/harnesses"),
                    str(profiles),
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "profile_invalid_encoding"):
                load_workflow(workflow)


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
