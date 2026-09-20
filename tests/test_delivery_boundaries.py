"""Boundary coverage for the delivery stack's untested guards.

Three surfaces that had no direct tests:
  - ``HerdrDeliveryDispatcher.inspect_agent`` — the identity check that must
    reject stale/foreign agents before a resume is trusted.
  - ``StandardizedDelivery.run``/``__init__`` — goal-file and lease validation
    that fails before any journal claim is written.
  - ``GitWorkspace`` — worktree-path confinement and git error mapping.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.delivery import (
    DeliveryError,
    HerdrDeliveryDispatcher,
    StandardizedDelivery,
)
from herdr_orchestrator.git_workspace import GitWorkspace, GitWorkspaceError, Worktree
from herdr_orchestrator.model import AgentState, DispatchOutcome, Harness, TrackerBackend
from herdr_orchestrator.protocol import TransportError
from herdr_orchestrator.tracker import LocalMarkdownTracker

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def _init_repository(repository: Path) -> str:
    repository.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "chore: initialize")
    return _git(repository, "rev-parse", "HEAD").stdout.strip()


def _agent_payload(workspace: Path, **overrides: object) -> dict[str, object]:
    agent: dict[str, object] = {
        "name": "owned-codex",
        "agent": "codex",
        "agent_status": AgentState.IDLE.value,
        "pane_id": "w1:p2",
        "workspace_id": "w1",
        "cwd": str(workspace),
        "foreground_cwd": str(workspace),
        "interactive_ready": True,
    }
    agent.update(overrides)
    return agent


class InspectAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()
        self.config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        self.dispatcher = HerdrDeliveryDispatcher(self.config)

    def _inspect(self, payload: dict[str, object]) -> DispatchOutcome | None:
        with patch("herdr_orchestrator.delivery.run_json", return_value={"agent": payload}):
            return self.dispatcher.inspect_agent(self.workspace, "owned-codex", Harness.CODEX)

    def test_returns_outcome_for_a_matching_interactive_agent(self) -> None:
        outcome = self._inspect(_agent_payload(self.workspace))
        assert outcome is not None
        self.assertEqual(outcome.agent_name, "owned-codex")
        self.assertEqual(outcome.state, AgentState.IDLE)
        self.assertEqual(outcome.pane_id, "w1:p2")
        self.assertTrue(outcome.agent_settled)
        self.assertEqual(outcome.execution_path, str(self.workspace))
        self.assertEqual(outcome.herdr_workspace_id, "w1")

    def test_working_agent_is_not_settled(self) -> None:
        outcome = self._inspect(
            _agent_payload(self.workspace, agent_status=AgentState.WORKING.value)
        )
        assert outcome is not None
        self.assertFalse(outcome.agent_settled)

    def test_agent_not_found_maps_to_none(self) -> None:
        with patch(
            "herdr_orchestrator.delivery.run_json",
            side_effect=TransportError("agent_not_found"),
        ):
            self.assertIsNone(
                self.dispatcher.inspect_agent(self.workspace, "gone-agent", Harness.CODEX)
            )

    def test_other_transport_errors_propagate(self) -> None:
        with (
            patch(
                "herdr_orchestrator.delivery.run_json",
                side_effect=TransportError("herdr_timeout"),
            ),
            self.assertRaisesRegex(TransportError, "herdr_timeout"),
        ):
            self.dispatcher.inspect_agent(self.workspace, "a", Harness.CODEX)

    def test_non_dict_agent_is_invalid_response(self) -> None:
        with (
            patch("herdr_orchestrator.delivery.run_json", return_value={"agent": ["not-a-dict"]}),
            self.assertRaisesRegex(TransportError, "herdr_invalid_response"),
        ):
            self.dispatcher.inspect_agent(self.workspace, "a", Harness.CODEX)

    def test_unknown_state_value_is_invalid_response(self) -> None:
        with self.assertRaisesRegex(TransportError, "herdr_invalid_response"):
            self._inspect(_agent_payload(self.workspace, agent_status="teleported"))

    def test_identity_mismatch_cases(self) -> None:
        other = self.workspace / "elsewhere"
        other.mkdir()
        cases = {
            "foreign agent name": {"name": "someone-else"},
            "wrong harness": {"agent": "droid"},
            "empty pane": {"pane_id": ""},
            "non-string pane": {"pane_id": 7},
            "not interactive": {"interactive_ready": False},
            "interactive flag wrong type": {"interactive_ready": "yes"},
            "cwd outside workspace": {"cwd": str(other)},
            "foreground cwd outside workspace": {"foreground_cwd": str(other)},
            "blank workspace id": {"workspace_id": ""},
            "non-string workspace id": {"workspace_id": 3},
        }
        for label, override in cases.items():
            with (
                self.subTest(label),
                self.assertRaisesRegex(TransportError, "agent_identity_mismatch"),
            ):
                self._inspect(_agent_payload(self.workspace, **override))

    def test_optional_fields_may_be_absent(self) -> None:
        payload = _agent_payload(self.workspace)
        del payload["name"]
        del payload["workspace_id"]
        outcome = self._inspect(payload)
        assert outcome is not None
        self.assertIsNone(outcome.herdr_workspace_id)

    def test_transport_is_cached_per_workspace(self) -> None:
        first = self.dispatcher._transport(self.workspace)
        second = self.dispatcher._transport(self.workspace / ".")
        self.assertIs(first, second)
        other = self.workspace / "sibling"
        other.mkdir()
        self.assertIsNot(first, self.dispatcher._transport(other))


class DeliveryInitAndRunBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        self.delivery_config = replace(
            config.standardized_delivery,
            artifact_root=self.root / ".orchestrator/deliveries",
            tracker_root=self.root / ".scratch/delivery",
        )
        self.config = replace(config, standardized_delivery=self.delivery_config)
        self.tracker = LocalMarkdownTracker(self.delivery_config.tracker_root)

    def _delivery(self, **kwargs: object) -> StandardizedDelivery:
        defaults: dict[str, object] = {
            "dispatcher": Mock(),
            "tracker": self.tracker,
            "controller_harness": Harness.DROID,
            "worker_harnesses": (Harness.DROID,),
        }
        defaults.update(kwargs)
        return StandardizedDelivery(self.config, **defaults)  # type: ignore[arg-type]

    def test_lease_seconds_must_be_finite_and_positive(self) -> None:
        for bad in (0, -1, float("nan"), float("inf"), -float("inf")):
            with (
                self.subTest(bad),
                self.assertRaisesRegex(DeliveryError, "delivery_lease_seconds_invalid"),
            ):
                self._delivery(lease_seconds=bad)

    def test_missing_goal_file_is_rejected(self) -> None:
        delivery = self._delivery()
        with self.assertRaisesRegex(DeliveryError, "delivery_goal_not_found"):
            delivery.run(self.root / "no-such-goal.md")

    def test_empty_goal_is_rejected(self) -> None:
        goal = self.root / "goal.md"
        goal.write_text("   \n", encoding="utf-8")
        with self.assertRaisesRegex(DeliveryError, "delivery_goal_empty"):
            self._delivery().run(goal)

    def test_github_tracker_rejects_secret_material_in_goal(self) -> None:
        github_delivery = replace(self.delivery_config, tracker_backend=TrackerBackend.GITHUB)
        config = replace(self.config, standardized_delivery=github_delivery)
        goal = self.root / "goal.md"
        goal.write_text("Ship the thing. Token: ghp_" + "a1" * 18 + "\n", encoding="utf-8")
        delivery = StandardizedDelivery(
            config,
            dispatcher=Mock(),
            tracker=self.tracker,
            controller_harness=Harness.DROID,
            worker_harnesses=(Harness.DROID,),
        )
        with self.assertRaisesRegex(DeliveryError, "delivery_secret_material_rejected"):
            delivery.run(goal)


class GitWorkspaceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repository = self.root / "repository"
        self.base_commit = _init_repository(self.repository)
        self.runtime = self.root / "runtime"
        self.workspace = GitWorkspace(self.repository, self.runtime, "delivery")

    def test_validate_commit_rejects_dirty_worktree(self) -> None:
        ticket = self.workspace.create_ticket("t1", base_commit=self.base_commit)
        (ticket.path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        with self.assertRaisesRegex(GitWorkspaceError, "worktree_dirty"):
            self.workspace.validate_commit(ticket)

    def test_validate_commit_rejects_missing_commit(self) -> None:
        ticket = self.workspace.create_ticket("t2", base_commit=self.base_commit)
        with self.assertRaisesRegex(GitWorkspaceError, "worktree_commit_missing"):
            self.workspace.validate_commit(ticket)

    def test_create_rejects_invalid_branch_names(self) -> None:
        for branch in ("", "a b", "a;b", "ho/$delivery", "wild*card"):
            with (
                self.subTest(branch),
                self.assertRaisesRegex(GitWorkspaceError, "worktree_branch_invalid"),
            ):
                self.workspace._create(self.runtime / "wt" / "x", branch, self.base_commit)

    def test_create_rejects_existing_path_on_another_branch(self) -> None:
        existing = self.runtime / "worktrees" / "taken"
        existing.mkdir(parents=True)
        _git(
            self.repository,
            "worktree",
            "add",
            "-b",
            "other-branch",
            str(existing),
            self.base_commit,
        )
        with self.assertRaisesRegex(GitWorkspaceError, "worktree_path_conflict"):
            self.workspace._create(existing, "wanted-branch", self.base_commit)

    def test_merge_failure_maps_to_named_error(self) -> None:
        integration = self.workspace.create_integration(self.base_commit)
        ticket = self.workspace.create_ticket("t3", base_commit=self.base_commit)
        (integration.path / "README.md").write_text("integration\n", encoding="utf-8")
        _git(integration.path, "add", "README.md")
        _git(integration.path, "commit", "-m", "integration change")
        (ticket.path / "README.md").write_text("conflicting\n", encoding="utf-8")
        _git(ticket.path, "add", "README.md")
        _git(ticket.path, "commit", "-m", "conflicting change")
        with self.assertRaisesRegex(GitWorkspaceError, "ticket_merge_failed"):
            self.workspace.merge(integration, ticket)

    def test_observe_worktree_returns_none_for_missing_path(self) -> None:
        self.assertIsNone(
            self.workspace.observe_worktree(
                self.runtime / "worktrees" / "absent",
                "ho/delivery/absent",
                self.base_commit,
                require_base_head=False,
            )
        )

    def test_observe_worktree_rejects_moved_head(self) -> None:
        ticket = self.workspace.create_ticket("t4", base_commit=self.base_commit)
        (ticket.path / "moved.txt").write_text("x\n", encoding="utf-8")
        _git(ticket.path, "add", "moved.txt")
        _git(ticket.path, "commit", "-m", "moved head")
        with self.assertRaisesRegex(GitWorkspaceError, "delivery_worktree_create_conflict"):
            self.workspace.observe_worktree(
                ticket.path, ticket.branch, self.base_commit, require_base_head=True
            )

    def test_worktree_paths_must_stay_under_runtime_root(self) -> None:
        outside = self.root / "escape"
        with self.assertRaisesRegex(GitWorkspaceError, "worktree_path_invalid"):
            self.workspace._create(outside, "ok-branch", self.base_commit)

    def test_git_cwd_is_confined_to_repository_or_runtime(self) -> None:
        with self.assertRaisesRegex(GitWorkspaceError, "delivery_git_query_failed"):
            self.workspace.output(self.root / "unrelated-dir", "status")

    def test_output_wraps_failing_git_calls(self) -> None:
        with self.assertRaisesRegex(GitWorkspaceError, "delivery_git_query_failed"):
            self.workspace.output(self.repository, "cat-file", "-t", "0" * 40)

    def test_parents_rejects_unresolvable_commit(self) -> None:
        with self.assertRaisesRegex(GitWorkspaceError, "delivery_git_query_failed"):
            self.workspace.parents(self.repository, "0" * 40)

    def test_validate_ownership_rejects_foreign_path(self) -> None:
        other_root = self.root / "other-runtime"
        foreign = other_root / "worktrees" / "f"
        foreign.mkdir(parents=True)
        _git(
            self.repository,
            "worktree",
            "add",
            "-b",
            "foreign-branch",
            str(foreign),
            self.base_commit,
        )
        # A worktree that lives outside this workspace's runtime root.
        with self.assertRaisesRegex(GitWorkspaceError, "delivery_worktree_ownership_invalid"):
            self.workspace.validate_ownership(
                foreign, Worktree(foreign, "foreign-branch", self.base_commit)
            )

    def test_git_runner_timeout_and_oserror_are_mapped(self) -> None:
        def timeout_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(["git"], 120)

        def oom_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise OSError("no git")

        runners = ((timeout_runner, "git_command_timeout"), (oom_runner, "git_unavailable"))
        for runner, code in runners:
            ws = GitWorkspace(
                self.repository,
                self.runtime,
                "delivery",
                runner=runner,  # type: ignore[arg-type]
            )
            with self.subTest(code), self.assertRaisesRegex(GitWorkspaceError, code):
                ws.base_commit()


if __name__ == "__main__":
    unittest.main()
