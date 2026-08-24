from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.model import Harness
from herdr_orchestrator.selection import (
    effective_worker_harnesses,
    select_controller_harness,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")

    def test_auto_controller_uses_first_installed_preferred_harness(self) -> None:
        available = {"grok", "codex"}

        selected = select_controller_harness(
            self.config,
            worker_harnesses=(Harness.GROK, Harness.CODEX),
            executable_finder=lambda command: command if command in available else None,
        )

        self.assertEqual(selected, Harness.GROK)

    def test_explicit_controller_overrides_auto_selection(self) -> None:
        selected = select_controller_harness(
            self.config,
            worker_harnesses=(Harness.GROK, Harness.CODEX),
            override=Harness.CODEX,
            executable_finder=lambda _: None,
        )

        self.assertEqual(selected, Harness.CODEX)

    def test_force_auto_overrides_configured_controller(self) -> None:
        config = replace(
            self.config,
            planner=replace(self.config.planner, harness=Harness.CLAUDE),
        )

        selected = select_controller_harness(
            config,
            worker_harnesses=(Harness.GROK, Harness.CODEX),
            force_auto=True,
            executable_finder=lambda command: command,
        )

        self.assertEqual(selected, Harness.GROK)

    def test_worker_override_rejects_unconfigured_harness(self) -> None:
        config = replace(
            self.config,
            workers=tuple(
                worker
                for worker in self.config.workers
                if worker.harness is not Harness.CLAUDE
            ),
        )

        with self.assertRaisesRegex(ValueError, "has_no_worker"):
            effective_worker_harnesses(config, (Harness.CLAUDE,))


if __name__ == "__main__":
    unittest.main()
