from __future__ import annotations

import unittest

from herdr_orchestrator.cli import build_parser


class CliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
