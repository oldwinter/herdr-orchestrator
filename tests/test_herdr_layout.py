from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.herdr_layout import HerdrLayout
from herdr_orchestrator.model import DispatchContext, PlacementTarget


class FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: int | None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if not self.responses:
            raise AssertionError(f"unexpected call: {argv}")
        return self.responses.pop(0)


class HerdrLayoutTests(unittest.TestCase):
    def test_tab_uses_short_task_label_not_agent_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "root_pane": {"pane_id": "w1:p2"},
                            "tab": {"tab_id": "w1:t2"},
                        }
                    )
                ]
            )
            layout = HerdrLayout("example", workspace, "w1", runner)

            terminal = layout.provision(
                DispatchContext(
                    PlacementTarget.TAB,
                    "Review the architecture",
                    "review-v1",
                )
            )

        self.assertEqual(terminal.pane_id, "w1:p2")
        label_index = runner.calls[0].index("--label") + 1
        self.assertEqual(runner.calls[0][label_index], "Review the architecture")
        self.assertNotRegex(runner.calls[0][label_index], r"[a-f0-9]{6,8}$")

    def test_pane_tasks_share_one_batch_tab_and_split_largest_pane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner(
                [
                    _result(
                        {
                            "root_pane": {"pane_id": "w1:p2"},
                            "tab": {"tab_id": "w1:t2"},
                        }
                    ),
                    _result(
                        {
                            "layout": {
                                "panes": [
                                    {
                                        "pane_id": "w1:p2",
                                        "rect": {"width": 140, "height": 50},
                                    }
                                ]
                            }
                        }
                    ),
                    _result({"pane": {"pane_id": "w1:p3"}}),
                ]
            )
            layout = HerdrLayout("example", workspace, "w1", runner)
            first = DispatchContext(
                PlacementTarget.PANE,
                "Inspect one",
                "one",
                batch_key="run-1",
            )
            second = DispatchContext(
                PlacementTarget.PANE,
                "Inspect two",
                "two",
                batch_key="run-1",
            )

            first_terminal = layout.provision(first)
            second_terminal = layout.provision(second)

        self.assertEqual(first_terminal.tab_id, second_terminal.tab_id)
        self.assertEqual(
            sum(call[0:3] == ["herdr", "tab", "create"] for call in runner.calls),
            1,
        )
        split = runner.calls[2]
        self.assertEqual(split[0:3], ["herdr", "pane", "split"])
        self.assertEqual(split[split.index("--direction") + 1], "right")

    def test_reused_tab_refreshes_visible_label_without_changing_agent_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runner = FakeRunner([_result({"type": "ok"})])
            layout = HerdrLayout("example", workspace, "w1", runner)
            agent = {
                "name": "ho-codex-8def7c21",
                "tab_id": "w1:t2",
                "workspace_id": "w1",
            }

            layout.refresh_visible_label(
                agent,
                DispatchContext(
                    PlacementTarget.TAB,
                    "Implement concise labels",
                    "labels-v1",
                ),
            )

        self.assertEqual(
            runner.calls[0],
            [
                "herdr",
                "tab",
                "rename",
                "w1:t2",
                "Implement concise labels",
            ],
        )
        self.assertEqual(agent["name"], "ho-codex-8def7c21")

    def test_failed_batch_root_is_removed_from_layout_cache(self) -> None:
        created = {
            "root_pane": {"pane_id": "w1:p2"},
            "tab": {"tab_id": "w1:t2"},
        }
        recreated = {
            "root_pane": {"pane_id": "w1:p3"},
            "tab": {"tab_id": "w1:t3"},
        }
        runner = FakeRunner(
            [
                _result(created),
                _result({"type": "ok"}),
                _result(recreated),
            ]
        )
        layout = HerdrLayout("example", Path("/repo"), "w1", runner)
        context = DispatchContext(
            PlacementTarget.PANE,
            "Inspect",
            "inspect-v1",
            batch_key="run-1",
        )

        failed = layout.provision(context)
        layout.cleanup_failed(failed)
        retried = layout.provision(context)

        self.assertEqual(retried.tab_id, "w1:t3")
        creates = [call for call in runner.calls if call[:3] == ["herdr", "tab", "create"]]
        self.assertEqual(len(creates), 2)
        self.assertFalse(any(call[:3] == ["herdr", "pane", "layout"] for call in runner.calls))

    def test_worktree_uses_native_create_and_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / ".orchestrator/worktrees"
            runner = FakeRunner(
                [
                    _result(
                        {
                            "workspace": {"workspace_id": "w2"},
                            "tab": {"tab_id": "w2:t1"},
                            "root_pane": {"pane_id": "w2:p1"},
                        }
                    )
                ]
            )
            layout = HerdrLayout("example", workspace, "w1", runner)
            context = DispatchContext(
                PlacementTarget.WORKTREE,
                "Implement topology",
                "topology-v1",
                worktree_root=root,
            )

            terminal = layout.provision(context)
            layout.close_temporary(terminal)

        command = runner.calls[0]
        self.assertEqual(command[0:3], ["herdr", "worktree", "create"])
        self.assertIn("--branch", command)
        self.assertIn("--path", command)
        self.assertEqual(terminal.cwd, layout.execution_workspace(context))
        self.assertEqual(len(runner.calls), 1)


def _result(result: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["herdr"],
        0,
        json.dumps({"id": "test", "result": result}),
        "",
    )


if __name__ == "__main__":
    unittest.main()
