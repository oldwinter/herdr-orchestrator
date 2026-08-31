from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.model import PlacementMode, PlacementTarget
from herdr_orchestrator.topology import (
    TopologyDecisionError,
    load_topology_decision,
    short_display_label,
    static_placement,
    topology_decision_prompt,
)


class TopologyTests(unittest.TestCase):
    def test_hybrid_routes_read_only_to_batch_pane(self) -> None:
        placement = static_placement(
            PlacementMode.HYBRID,
            "Review configuration",
            "Read-only inspection. Do not modify files.",
            supports_worktree=True,
        )

        self.assertEqual(placement, PlacementTarget.PANE)

    def test_hybrid_routes_repository_writes_to_worktree(self) -> None:
        placement = static_placement(
            PlacementMode.HYBRID,
            "Implement topology",
            "Modify the coordinator and add tests.",
            supports_worktree=True,
        )

        self.assertEqual(placement, PlacementTarget.WORKTREE)

    def test_explicit_override_precedes_static_signals(self) -> None:
        placement = static_placement(
            PlacementMode.HYBRID,
            "Implement topology",
            "Modify the coordinator.",
            override=PlacementTarget.TAB,
            supports_worktree=True,
        )

        self.assertEqual(placement, PlacementTarget.TAB)

    def test_ambiguous_hybrid_task_requires_controller_decision(self) -> None:
        placement = static_placement(
            PlacementMode.HYBRID,
            "Investigate next step",
            "Determine the best execution approach.",
            supports_worktree=True,
        )

        self.assertIsNone(placement)

    def test_hybrid_does_not_match_write_signal_inside_word(self) -> None:
        placement = static_placement(
            PlacementMode.HYBRID,
            "Prefix audit",
            "Review the existing behavior.",
            supports_worktree=True,
        )

        self.assertEqual(placement, PlacementTarget.PANE)

    def test_controller_decision_is_strict_and_git_aware(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "topology.json"
            output.write_text(
                json.dumps(
                    {
                        "placement": "worktree",
                        "rationale": "The task writes repository files.",
                    }
                ),
                encoding="utf-8",
            )

            placement = load_topology_decision(
                output,
                supports_worktree=True,
            )
            prompt = topology_decision_prompt(
                "Task",
                "Ambiguous task.",
                output,
                supports_worktree=False,
            )

        self.assertEqual(placement, PlacementTarget.WORKTREE)
        self.assertNotIn("worktree: repository-writing", prompt)
        self.assertNotIn('"placement":"tab|pane|worktree"', prompt)

    def test_rejects_worktree_without_git_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "topology.json"
            output.write_text(
                '{"placement":"worktree","rationale":"Needs isolation."}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                TopologyDecisionError,
                "topology_worktree_requires_git",
            ):
                load_topology_decision(output, supports_worktree=False)

    def test_rejects_duplicate_controller_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "topology.json"
            output.write_text(
                '{"placement":"pane","placement":"worktree","rationale":"Ambiguous."}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                TopologyDecisionError,
                "topology_output_duplicate_key",
            ):
                load_topology_decision(output, supports_worktree=True)

    def test_rejects_unreadable_controller_output(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(
                TopologyDecisionError,
                "topology_output_unreadable",
            ),
        ):
            load_topology_decision(Path(temporary), supports_worktree=True)

    def test_display_label_truncates_without_hash_suffix(self) -> None:
        label = short_display_label(
            "Implement a very long topology decision title for this workflow",
            fallback="task",
            maximum=24,
        )

        self.assertEqual(len(label), 24)
        self.assertTrue(label.endswith("…"))
        self.assertNotRegex(label, r"-[a-f0-9]{6,8}$")


if __name__ == "__main__":
    unittest.main()
