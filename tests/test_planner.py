from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.model import Harness
from herdr_orchestrator.planner import PlannerOutputError, load_planner_tasks


class PlannerTests(unittest.TestCase):
    def test_loads_valid_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "title": "Review config",
                                "harness": "claude",
                                "prompt": "Read only.",
                                "dedupe_key": "review-config-v1",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            tasks = load_planner_tasks(path, max_tasks=10)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].harness, Harness.CLAUDE)

    def test_rejects_shell_command_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "title": "Unsafe",
                                "harness": "codex",
                                "prompt": "Run this.",
                                "dedupe_key": "unsafe-v1",
                                "command": "rm -rf /",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PlannerOutputError, "invalid_shape"):
                load_planner_tasks(path, max_tasks=10)

    def test_rejects_duplicate_dedupe_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            task = {
                "title": "One",
                "harness": "pi",
                "prompt": "Inspect.",
                "dedupe_key": "same",
            }
            path.write_text(json.dumps({"tasks": [task, task]}), encoding="utf-8")

            with self.assertRaisesRegex(PlannerOutputError, "duplicate"):
                load_planner_tasks(path, max_tasks=10)


if __name__ == "__main__":
    unittest.main()
