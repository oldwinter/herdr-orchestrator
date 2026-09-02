from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.model import Harness
from herdr_orchestrator.planner import (
    MAX_PLANNER_OUTPUT_BYTES,
    PlannerOutputError,
    load_planner_tasks,
    load_worker_selection,
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

    def test_rejects_shell_authority_field_aliases(self) -> None:
        task = {
            "title": "Unsafe",
            "harness": "codex",
            "prompt": "Run this.",
            "dedupe_key": "unsafe-v1",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            for key in ("shell_command", "argv"):
                path.write_text(
                    json.dumps({"tasks": [{**task, key: ["sh", "-c", "echo unsafe"]}]}),
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

    def test_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(
                '{"tasks":[],"tasks":[{"title":"T","harness":"codex",'
                '"prompt":"P","dedupe_key":"k"}]}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PlannerOutputError, "planner_output_duplicate_key"):
                load_planner_tasks(path, max_tasks=10)

    def test_rejects_duplicate_task_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(
                '{"tasks":[{"title":"T","harness":"codex","prompt":"P",'
                '"dedupe_key":"k","dedupe_key":"other"}]}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PlannerOutputError, "planner_output_duplicate_key"):
                load_planner_tasks(path, max_tasks=10)

    def test_rejects_invalid_utf8_planner_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_bytes(b'{"tasks": []}\xff')

            with self.assertRaisesRegex(PlannerOutputError, "planner_output_unreadable"):
                load_planner_tasks(path, max_tasks=10)

    def test_rejects_symlinked_planner_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text('{"tasks": []}', encoding="utf-8")
            path = root / "plan.json"
            path.symlink_to(target)

            with self.assertRaisesRegex(PlannerOutputError, "planner_output_path_invalid"):
                load_planner_tasks(path, max_tasks=10)

    def test_rejects_unreadable_planner_output(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(PlannerOutputError, "planner_output_unreadable"),
        ):
            load_planner_tasks(Path(temporary), max_tasks=10)

    def test_rejects_oversized_planner_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            with path.open("wb") as output:
                output.truncate(MAX_PLANNER_OUTPUT_BYTES + 1)

            with self.assertRaisesRegex(PlannerOutputError, "planner_output_too_large"):
                load_planner_tasks(path, max_tasks=10)

    def test_rejects_invalid_max_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text('{"tasks": []}', encoding="utf-8")

            with self.assertRaisesRegex(PlannerOutputError, "planner_max_tasks_invalid"):
                load_planner_tasks(path, max_tasks=0)

    def test_enforces_task_count_limit(self) -> None:
        task = {
            "title": "T",
            "harness": "codex",
            "prompt": "P",
            "dedupe_key": "k",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            task,
                            {**task, "dedupe_key": "other"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            tasks = load_planner_tasks(path, max_tasks=2)
            self.assertEqual(len(tasks), 2)

            with self.assertRaisesRegex(PlannerOutputError, "planner_tasks_invalid"):
                load_planner_tasks(path, max_tasks=1)

    def test_rejects_unsafe_prompt_output_path(self) -> None:
        with self.assertRaisesRegex(PlannerOutputError, "planner_output_path_invalid"):
            planner_prompt(
                "Plan work.",
                Path("plans/../outside.json"),
                3,
                "{}",
                (Harness.CODEX,),
            )

        with self.assertRaisesRegex(
            PlannerOutputError,
            "worker_selection_output_path_invalid",
        ):
            worker_selection_prompt(
                "Route work.",
                Path("route\nforged.json"),
                "{}",
                (Harness.CODEX,),
            )

    def test_worker_selection_rejects_duplicate_keys_and_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"harness":"codex","harness":"grok"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PlannerOutputError,
                "worker_selection_duplicate_key",
            ):
                load_worker_selection(
                    duplicate,
                    allowed_harnesses=(Harness.CODEX, Harness.GROK),
                )

            invalid = root / "invalid.json"
            invalid.write_bytes(b'{"harness":"codex"}\xff')
            with self.assertRaisesRegex(PlannerOutputError, "worker_selection_unreadable"):
                load_worker_selection(
                    invalid,
                    allowed_harnesses=(Harness.CODEX,),
                )

    def test_rejects_nul_in_task_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "title": "T",
                                "harness": "codex",
                                "prompt": "before\x00after",
                                "dedupe_key": "k",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PlannerOutputError, "planner_prompt_invalid"):
                load_planner_tasks(path, max_tasks=10)


if __name__ == "__main__":
    unittest.main()
