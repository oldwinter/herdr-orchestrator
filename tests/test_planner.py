from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.model import Harness
from herdr_orchestrator.planner import (
    PlannerOutputError,
    load_worker_selection,
    load_planner_tasks,
    planner_prompt,
    worker_selection_prompt,
)


class PlannerTests(unittest.TestCase):
    def test_prompt_exposes_compact_catalog_and_dynamic_selection_rule(self) -> None:
        prompt = planner_prompt(
            "Plan work.",
            Path("/tmp/plan.json"),
            3,
            '{"harnesses":[{"harness":"codex","summary":"coding"}]}',
            (Harness.CODEX,),
        )

        self.assertIn('"harness":"codex"', prompt)
        self.assertNotIn("claude", prompt)
        self.assertNotIn("hermes", prompt)
        self.assertIn("按需加载所选 harness 的完整 profile", prompt)
        self.assertIn("/tmp/plan.json", prompt)

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

    def test_worker_selection_is_limited_to_allowed_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text('{"harness":"grok"}', encoding="utf-8")

            selected = load_worker_selection(
                path,
                allowed_harnesses=(Harness.GROK, Harness.CODEX),
            )
            prompt = worker_selection_prompt(
                "Implement the feature.",
                path,
                '{"harnesses":[{"harness":"grok"}]}',
                (Harness.GROK, Harness.CODEX),
            )

        self.assertEqual(selected, Harness.GROK)
        self.assertIn('"harness":"grok|codex"', prompt)
        self.assertNotIn("claude", prompt)

    def test_rejects_worker_selection_outside_allowed_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text('{"harness":"claude"}', encoding="utf-8")

            with self.assertRaisesRegex(PlannerOutputError, "not_allowed"):
                load_worker_selection(
                    path,
                    allowed_harnesses=(Harness.GROK, Harness.CODEX),
                )

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
