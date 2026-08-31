from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.delivery import (
    DeliveryError,
    DeliveryEscalation,
    StandardizedDelivery,
    _delivery_run_claim,
)
from herdr_orchestrator.delivery_protocol import (
    FindingSeverity,
    ReviewFinding,
    ReviewReport,
    load_delivery_plan,
)
from herdr_orchestrator.git_workspace import GitWorkspace, Worktree
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    WayfinderMode,
)
from herdr_orchestrator.tracker import LocalMarkdownTracker

REPO_ROOT = Path(__file__).resolve().parents[1]


class ScriptedDeliveryDispatcher:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active_implementations = 0
        self.max_active_implementations = 0
        self.prompts: list[str] = []

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        with self.lock:
            self.prompts.append(prompt)
        if "Create one accepted specification" in prompt:
            _artifact_path(prompt).write_text(
                json.dumps(_delivery_plan()),
                encoding="utf-8",
            )
        elif "受限 harness router" in prompt:
            _artifact_path(prompt).write_text(
                json.dumps({"harness": "droid"}),
                encoding="utf-8",
            )
        elif "Implement exactly one accepted delivery ticket" in prompt:
            self._implement(workspace, prompt)
        elif "Standards axis only" in prompt:
            _artifact_path(prompt).write_text(
                json.dumps({"standards": []}),
                encoding="utf-8",
            )
        elif "Spec axis only" in prompt:
            _artifact_path(prompt).write_text(
                json.dumps({"spec": []}),
                encoding="utf-8",
            )
        else:
            raise AssertionError(f"unexpected delivery prompt: {prompt[:120]}")
        return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("no scripted worker should block")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("no scripted worker should block")

    def _implement(self, workspace: Path, prompt: str) -> None:
        receipt = _receipt_path(prompt)
        ticket_id = re.search(r"ticket-(\d{2})\.json\Z", receipt.name)
        assert ticket_id is not None
        identifier = ticket_id.group(1)
        with self.lock:
            self.active_implementations += 1
            self.max_active_implementations = max(
                self.max_active_implementations,
                self.active_implementations,
            )
        try:
            time.sleep(0.05)
            changed = workspace / f"slice-{identifier}.txt"
            changed.write_text(f"ticket {identifier}\n", encoding="utf-8")
            _git(workspace, "add", changed.name)
            _git(workspace, "commit", "-m", f"feat: implement slice {identifier}")
            commit = _git(workspace, "rev-parse", "HEAD").stdout.strip()
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                json.dumps(
                    {
                        "ticket_id": identifier,
                        "commit": commit,
                        "acceptance": [
                            {
                                "criterion": f"Slice {identifier} works.",
                                "passed": True,
                                "evidence": f"slice-{identifier}.txt exists",
                            }
                        ],
                        "checks": ["scripted validation passed"],
                        "summary": f"Implemented slice {identifier}.",
                    }
                ),
                encoding="utf-8",
            )
        finally:
            with self.lock:
                self.active_implementations -= 1


class BlockedDispatcher:
    def __init__(self, question: str) -> None:
        self.question = question
        self.dispatch_calls = 0
        self.responses: list[str] = []

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        self.dispatch_calls += 1
        if self.dispatch_calls == 1:
            return DispatchOutcome(
                agent_name,
                AgentState.BLOCKED,
                False,
                "w1:p2",
                "agent_blocked",
            )
        _artifact_path(prompt).write_text(
            json.dumps(
                {
                    "action": "answer",
                    "category": "spec-authorized",
                    "response": "Use the accepted default.",
                    "rationale": "The spec already fixes this choice.",
                }
            ),
            encoding="utf-8",
        )
        return DispatchOutcome(agent_name, AgentState.DONE, True, "w1:p3")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        return self.question

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        self.responses.append(response)
        return DispatchOutcome(name, AgentState.DONE, True, "w1:p2")


class WayfinderDispatcher:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        self.prompts.append(prompt)
        output = _artifact_path(prompt)
        if "Chart a Wayfinder decision map" in prompt:
            payload = {
                "destination": "A specification with the storage decision fixed.",
                "notes": ["Stay within the local repository."],
                "decisions": [
                    {
                        "id": "01",
                        "title": "Choose storage",
                        "question": "Which existing store should own the state?",
                        "kind": "research",
                        "blocked_by": [],
                        "resolution": "",
                    }
                ],
                "not_yet_specified": ["Ticket shape depends on the storage decision."],
                "out_of_scope": ["Production migration."],
            }
        elif "Resolve exactly one frontier decision" in prompt:
            payload = {
                "ticket_id": "01",
                "resolution": "Reuse the repository's existing SQLite store.",
                "new_decisions": [],
                "not_yet_specified": [],
                "out_of_scope": ["Production migration."],
            }
        else:
            raise AssertionError(f"unexpected Wayfinder prompt: {prompt[:120]}")
        output.write_text(json.dumps(payload), encoding="utf-8")
        return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("Wayfinder controller should not block")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("Wayfinder controller should not block")


class FlakyArtifactDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        self.calls += 1
        if self.calls == 2:
            _artifact_path(prompt).write_text(
                json.dumps({"use_wayfinder": False, "reason": "clear"}),
                encoding="utf-8",
            )
        return DispatchOutcome(agent_name, AgentState.DONE, True, "w1:p2")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("flaky artifact controller should not block")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("flaky artifact controller should not block")


class SettledWithoutArtifactDispatcher:
    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        return DispatchOutcome(agent_name, AgentState.DONE, True, "w1:p2")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("settled worker should not block")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("settled worker should not block")


class StandardizedDeliveryTests(unittest.TestCase):
    def test_runs_parallel_frontier_then_final_two_axis_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test User")
            _git(repository, "config", "user.email", "test@example.com")
            (repository / "README.md").write_text("test repo\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "-m", "chore: initialize")
            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=repository / ".orchestrator/deliveries",
                tracker_root=repository / ".scratch/delivery",
                wayfinder=WayfinderMode.NEVER,
                max_parallel=3,
                review_repair_rounds=2,
            )
            config = replace(
                config,
                workspace=repository,
                state_db=repository / ".orchestrator/state.db",
                standardized_delivery=delivery_config,
            )
            goal = repository / "goal.md"
            goal.write_text("Deliver three independent slices.", encoding="utf-8")
            dispatcher = ScriptedDeliveryDispatcher()
            tracker = LocalMarkdownTracker(delivery_config.tracker_root)
            delivery = StandardizedDelivery(
                config,
                dispatcher=dispatcher,
                tracker=tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )

            result = delivery.run(goal)
            dispatch_count = len(dispatcher.prompts)
            state_path = result.artifact_root / "state.json"
            state_path.write_text(
                json.dumps({"status": "running", "stage": "final-review"}),
                encoding="utf-8",
            )
            repeated = delivery.run(goal)
            reconciled_state = json.loads(state_path.read_text(encoding="utf-8"))
            dirty = result.artifact_root / "worktrees/integration/interrupted.txt"
            dirty.write_text("unfinished repair\n", encoding="utf-8")
            with self.assertRaisesRegex(DeliveryError, "worktree_dirty"):
                delivery.run(goal)
            dirty.unlink()
            result_path = result.artifact_root / "result.json"
            result_payload = json.loads(result_path.read_text(encoding="utf-8"))
            result_payload["integration_commit"] = "f" * 40
            result_path.write_text(json.dumps(result_payload), encoding="utf-8")
            with self.assertRaisesRegex(DeliveryError, "integration_commit"):
                delivery.run(goal)

            log = _git(
                result.artifact_root / "worktrees/integration",
                "log",
                "--oneline",
            ).stdout
            issues = list((delivery_config.tracker_root / "parallel-delivery/issues").glob("*.md"))
            issue_contents = [path.read_text(encoding="utf-8") for path in issues]
            ledger = (result.artifact_root / "decision-ledger.jsonl").read_text(encoding="utf-8")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(repeated, result)
        self.assertEqual(reconciled_state["status"], "succeeded")
        self.assertEqual(reconciled_state["stage"], "complete")
        self.assertEqual(len(dispatcher.prompts), dispatch_count)
        self.assertEqual(result.tickets_completed, 3)
        self.assertEqual(result.review_rounds, 1)
        self.assertGreaterEqual(dispatcher.max_active_implementations, 2)
        self.assertEqual(len(issues), 3)
        self.assertTrue(all("**Status:** completed" in text for text in issue_contents))
        self.assertIn("feat: implement slice 01", log)
        self.assertIn("feat: implement slice 02", log)
        self.assertIn("feat: implement slice 03", log)
        self.assertIn('"event": "review_completed"', ledger)
        review_prompts = [
            prompt
            for prompt in dispatcher.prompts
            if prompt.startswith("Review the committed diff")
        ]
        self.assertEqual(len(review_prompts), 2)
        self.assertTrue(all("do not delegate" in prompt for prompt in review_prompts))

    def test_principal_proxy_answers_spec_authorized_worker_question(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=root / ".orchestrator/deliveries",
                tracker_root=root / ".scratch/delivery",
            )
            config = replace(config, standardized_delivery=delivery_config)
            dispatcher = BlockedDispatcher("Which accepted local default should I use?")
            delivery = StandardizedDelivery(
                config,
                dispatcher=dispatcher,
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            delivery._goal = "Implement the accepted local behavior."
            delivery._run_root = delivery_config.artifact_root / "proxy-test"
            delivery._run_root.mkdir(parents=True)

            outcome = delivery._dispatch_with_proxy(
                root,
                Harness.DROID,
                "Implement it.",
                role="worker",
            )

            ledger = (delivery._run_root / "decision-ledger.jsonl").read_text(encoding="utf-8")

        self.assertEqual(outcome.state, AgentState.DONE)
        self.assertEqual(dispatcher.responses, ["Use the accepted default."])
        self.assertIn('"action": "answer"', ledger)
        self.assertNotIn("Use the accepted default.", ledger)

    def test_principal_proxy_escalates_sensitive_question_without_answering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=root / ".orchestrator/deliveries",
                tracker_root=root / ".scratch/delivery",
            )
            config = replace(config, standardized_delivery=delivery_config)
            dispatcher = BlockedDispatcher("Provide the production API token.")
            delivery = StandardizedDelivery(
                config,
                dispatcher=dispatcher,
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            delivery._goal = "Implement the accepted local behavior."
            delivery._run_root = delivery_config.artifact_root / "proxy-test"
            delivery._run_root.mkdir(parents=True)

            with self.assertRaisesRegex(DeliveryEscalation, "sensitive"):
                delivery._dispatch_with_proxy(
                    root,
                    Harness.DROID,
                    "Implement it.",
                    role="worker",
                )

        self.assertEqual(dispatcher.dispatch_calls, 1)
        self.assertEqual(dispatcher.responses, [])

    def test_wayfinder_resolves_frontier_before_returning_to_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=root / ".orchestrator/deliveries",
                tracker_root=root / ".scratch/delivery",
                wayfinder=WayfinderMode.ALWAYS,
            )
            config = replace(config, standardized_delivery=delivery_config)
            dispatcher = WayfinderDispatcher()
            delivery = StandardizedDelivery(
                config,
                dispatcher=dispatcher,
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            delivery._goal = "Resolve storage uncertainty, then specify the feature."
            delivery._run_root = delivery_config.artifact_root / "wayfinder-test"
            delivery._run_root.mkdir(parents=True)

            map_ = delivery._run_wayfinder()

        assert map_ is not None
        self.assertEqual(len(dispatcher.prompts), 2)
        self.assertEqual(
            map_.decisions[0].resolution,
            "Reuse the repository's existing SQLite store.",
        )
        self.assertEqual(map_.not_yet_specified, ())

    def test_resume_reuses_validated_plan_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=root / ".orchestrator/deliveries",
                tracker_root=root / ".scratch/delivery",
            )
            config = replace(config, standardized_delivery=delivery_config)
            dispatcher = ScriptedDeliveryDispatcher()
            delivery = StandardizedDelivery(
                config,
                dispatcher=dispatcher,
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            delivery._goal = "Deliver three independent slices."
            delivery._run_root = delivery_config.artifact_root / "resume-test"
            delivery._run_root.mkdir(parents=True)

            first = delivery._create_plan(None)
            second = delivery._create_plan(None)

        self.assertEqual(first, second)
        self.assertEqual(
            sum("Create one accepted specification" in prompt for prompt in dispatcher.prompts),
            1,
        )

    def test_resume_reuses_tracker_publication_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=root / ".orchestrator/deliveries",
                tracker_root=root / ".scratch/delivery",
            )
            config = replace(
                config,
                workspace=root,
                state_db=root / ".orchestrator/state.db",
                standardized_delivery=delivery_config,
            )
            plan_path = delivery_config.artifact_root / "publication" / "delivery-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps(_delivery_plan()), encoding="utf-8")
            plan = load_delivery_plan(plan_path)
            first = StandardizedDelivery(
                config,
                dispatcher=ScriptedDeliveryDispatcher(),
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            first._run_root = plan_path.parent
            published = first._publish_tracker(plan)
            second = StandardizedDelivery(
                config,
                dispatcher=ScriptedDeliveryDispatcher(),
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            second._run_root = first._run_root

            with patch.object(
                second.tracker,
                "publish",
                side_effect=AssertionError("tracker publish repeated"),
            ):
                recovered = second._publish_tracker(plan)

        self.assertEqual(
            {key: value.reference for key, value in recovered.items()},
            {key: value.reference for key, value in published.items()},
        )

    def test_missing_artifact_retries_once_on_same_ready_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=root / ".orchestrator/deliveries",
                tracker_root=root / ".scratch/delivery",
            )
            config = replace(config, standardized_delivery=delivery_config)
            dispatcher = FlakyArtifactDispatcher()
            delivery = StandardizedDelivery(
                config,
                dispatcher=dispatcher,
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            delivery._goal = "Route a clear task."
            delivery._run_root = delivery_config.artifact_root / "artifact-retry"
            delivery._run_root.mkdir(parents=True)
            output = delivery._run_root / "route.json"

            delivery._dispatch_artifact(
                root,
                Harness.DROID,
                f"Write only this UTF-8 JSON file:\n{output}",
                output,
                role="way-route",
            )

            ledger = (delivery._run_root / "decision-ledger.jsonl").read_text(encoding="utf-8")

        self.assertEqual(dispatcher.calls, 2)
        self.assertIn('"event": "artifact_prompt_retried"', ledger)

    def test_all_configured_repair_rounds_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test User")
            _git(repository, "config", "user.email", "test@example.com")
            (repository / "README.md").write_text("test repo\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "-m", "chore: initialize")

            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=repository / ".orchestrator/deliveries",
                tracker_root=repository / ".scratch/delivery",
                review_repair_rounds=2,
            )
            config = replace(
                config,
                workspace=repository,
                state_db=repository / ".orchestrator/state.db",
                standardized_delivery=delivery_config,
            )
            delivery = StandardizedDelivery(
                config,
                dispatcher=ScriptedDeliveryDispatcher(),
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            delivery._run_root = delivery_config.artifact_root / "repair-rounds"
            delivery._run_root.mkdir(parents=True)
            plan_path = delivery._run_root / "delivery-plan.json"
            plan_path.write_text(json.dumps(_delivery_plan()), encoding="utf-8")
            plan = load_delivery_plan(plan_path)
            git = GitWorkspace(repository, delivery._run_root, plan.slug)
            integration = git.create_integration(git.base_commit())
            finding = ReviewFinding(
                severity=FindingSeverity.MUST_FIX,
                summary="The review finding is real.",
                evidence="README.md:1",
                source="the accepted repository rule",
            )
            reports = iter(
                (
                    ReviewReport(standards=(finding,), spec=()),
                    ReviewReport(standards=(finding,), spec=()),
                    ReviewReport(standards=(), spec=()),
                )
            )
            repair_commits: list[str] = []

            def write_verdict(*args: object, **kwargs: object) -> None:
                verdict_file = args[3]
                assert isinstance(verdict_file, Path)
                verdict_file.parent.mkdir(parents=True, exist_ok=True)
                verdict_file.write_text(
                    json.dumps(
                        {
                            "accepted": ["standards:1"],
                            "dismissed": [],
                            "rationale": "The citation supports the finding.",
                        }
                    ),
                    encoding="utf-8",
                )

            def repair(*args: object, **kwargs: object) -> DispatchOutcome:
                marker = integration.path / f"repair-{len(repair_commits) + 1}.txt"
                marker.write_text("repaired\n", encoding="utf-8")
                _git(integration.path, "add", marker.name)
                _git(integration.path, "commit", "-m", "fix: repair finding")
                commit = _git(integration.path, "rev-parse", "HEAD").stdout.strip()
                repair_commits.append(commit)
                return DispatchOutcome("repair", AgentState.DONE, False, "w1:p2")

            with (
                patch.object(delivery, "_review", side_effect=lambda *a, **k: next(reports)),
                patch.object(delivery, "_select_worker", return_value=Harness.DROID),
                patch.object(delivery, "_dispatch_artifact", side_effect=write_verdict),
                patch.object(delivery, "_dispatch_with_proxy", side_effect=repair),
            ):
                rounds = delivery._review_and_repair(plan, integration)

        self.assertEqual(rounds, 3)
        self.assertEqual(len(repair_commits), 2)

    def test_review_does_not_reuse_stale_axis_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test User")
            _git(repository, "config", "user.email", "test@example.com")
            (repository / "README.md").write_text("test repo\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "-m", "chore: initialize")

            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=repository / ".orchestrator/deliveries",
                tracker_root=repository / ".scratch/delivery",
            )
            config = replace(
                config,
                workspace=repository,
                state_db=repository / ".orchestrator/state.db",
                standardized_delivery=delivery_config,
            )
            delivery = StandardizedDelivery(
                config,
                dispatcher=SettledWithoutArtifactDispatcher(),
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            delivery._run_root = delivery_config.artifact_root / "stale-review"
            delivery._run_root.mkdir(parents=True)
            plan_path = delivery._run_root / "delivery-plan.json"
            plan_path.write_text(json.dumps(_delivery_plan()), encoding="utf-8")
            plan = load_delivery_plan(plan_path)
            git = GitWorkspace(repository, delivery._run_root, plan.slug)
            integration = git.create_integration(git.base_commit())
            (integration.path / "implemented.txt").write_text("done\n", encoding="utf-8")
            _git(integration.path, "add", "implemented.txt")
            _git(integration.path, "commit", "-m", "feat: implement delivery")
            (delivery._run_root / "git-base.json").write_text(
                json.dumps(
                    {
                        "commit": _git(repository, "rev-parse", "HEAD").stdout.strip(),
                        "repository": str(repository.resolve()),
                    }
                ),
                encoding="utf-8",
            )
            review_root = delivery._run_root / "reviews/round-1"
            review_root.mkdir(parents=True)
            (review_root / "standards.json").write_text(
                json.dumps({"standards": []}),
                encoding="utf-8",
            )
            (review_root / "spec.json").write_text(
                json.dumps({"spec": []}),
                encoding="utf-8",
            )

            with (
                patch.object(delivery, "_select_worker", return_value=Harness.DROID),
                self.assertRaisesRegex(DeliveryError, "delivery_artifact_missing"),
            ):
                delivery._review(plan, integration, 1)

    def test_rejects_a_foreign_repository_at_ticket_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test User")
            _git(repository, "config", "user.email", "test@example.com")
            (repository / "README.md").write_text("test repo\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "-m", "chore: initialize")

            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=repository / ".orchestrator/deliveries",
                tracker_root=repository / ".scratch/delivery",
            )
            config = replace(
                config,
                workspace=repository,
                state_db=repository / ".orchestrator/state.db",
                standardized_delivery=delivery_config,
            )
            delivery = StandardizedDelivery(
                config,
                dispatcher=ScriptedDeliveryDispatcher(),
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            delivery._run_root = delivery_config.artifact_root / "foreign-worktree"
            delivery._run_root.mkdir(parents=True)
            plan_path = delivery._run_root / "delivery-plan.json"
            plan_path.write_text(json.dumps(_delivery_plan()), encoding="utf-8")
            plan = load_delivery_plan(plan_path)
            git = GitWorkspace(repository, delivery._run_root, plan.slug)
            base_commit = git.base_commit()
            foreign = delivery._run_root / "worktrees/ticket-01"
            foreign.mkdir(parents=True)
            _git(foreign, "init", "-b", "ho/parallel-delivery/ticket-01")
            _git(foreign, "config", "user.name", "Foreign User")
            _git(foreign, "config", "user.email", "foreign@example.com")
            (foreign / "foreign.txt").write_text("foreign\n", encoding="utf-8")
            _git(foreign, "add", "foreign.txt")
            _git(foreign, "commit", "-m", "foreign commit")
            foreign_commit = _git(foreign, "rev-parse", "HEAD").stdout.strip()
            receipt_path = delivery._run_root / "receipts/ticket-01.json"
            receipt_path.parent.mkdir(parents=True)
            receipt_path.write_text(
                json.dumps(
                    {
                        "ticket_id": "01",
                        "commit": foreign_commit,
                        "acceptance": [
                            {
                                "criterion": "Slice 01 works.",
                                "passed": True,
                                "evidence": "foreign.txt exists",
                            }
                        ],
                        "checks": ["foreign check"],
                        "summary": "Foreign receipt.",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DeliveryError, "worktree_ownership"):
                delivery._implement_ticket(
                    plan,
                    plan.tickets[0],
                    Harness.DROID,
                    Worktree(foreign, "ho/parallel-delivery/ticket-01", base_commit),
                )

    def test_interrupted_run_keeps_its_original_base_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test User")
            _git(repository, "config", "user.email", "test@example.com")
            (repository / "README.md").write_text("test repo\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "-m", "chore: initialize")
            original_base = _git(repository, "rev-parse", "HEAD").stdout.strip()

            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=repository / ".orchestrator/deliveries",
                tracker_root=repository / ".scratch/delivery",
                wayfinder=WayfinderMode.NEVER,
            )
            config = replace(
                config,
                workspace=repository,
                state_db=repository / ".orchestrator/state.db",
                standardized_delivery=delivery_config,
            )
            goal = repository / "goal.md"
            goal.write_text("Deliver three independent slices.", encoding="utf-8")

            class InterruptingDispatcher(ScriptedDeliveryDispatcher):
                def dispatch(self, *args: object, **kwargs: object) -> DispatchOutcome:
                    prompt = args[2]
                    assert isinstance(prompt, str)
                    if "Implement exactly one accepted delivery ticket" in prompt:
                        raise RuntimeError("interrupted during implementation")
                    return super().dispatch(*args, **kwargs)

            interrupted = StandardizedDelivery(
                config,
                dispatcher=InterruptingDispatcher(),
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )

            with self.assertRaisesRegex(RuntimeError, "interrupted during implementation"):
                interrupted.run(goal)

            (repository / "later.txt").write_text("later source change\n", encoding="utf-8")
            _git(repository, "add", "later.txt")
            _git(repository, "commit", "-m", "feat: advance source branch")
            advanced_head = _git(repository, "rev-parse", "HEAD").stdout.strip()
            resumed_dispatcher = ScriptedDeliveryDispatcher()
            resumed = StandardizedDelivery(
                config,
                dispatcher=resumed_dispatcher,
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )

            result = resumed.run(goal)
            integration = result.artifact_root / "worktrees/integration"
            merge_base = _git(integration, "merge-base", "HEAD", advanced_head).stdout.strip()
            review_prompts = [
                prompt
                for prompt in resumed_dispatcher.prompts
                if prompt.startswith("Review the committed diff")
            ]

        self.assertEqual(merge_base, original_base)
        self.assertNotEqual(merge_base, advanced_head)
        self.assertTrue(review_prompts)
        self.assertTrue(all(f"`{original_base}...HEAD`" in prompt for prompt in review_prompts))

    def test_repair_budget_survives_an_interrupted_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test User")
            _git(repository, "config", "user.email", "test@example.com")
            (repository / "README.md").write_text("test repo\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "-m", "chore: initialize")

            config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            delivery_config = replace(
                config.standardized_delivery,
                artifact_root=repository / ".orchestrator/deliveries",
                tracker_root=repository / ".scratch/delivery",
                review_repair_rounds=1,
            )
            config = replace(
                config,
                workspace=repository,
                state_db=repository / ".orchestrator/state.db",
                standardized_delivery=delivery_config,
            )
            delivery = StandardizedDelivery(
                config,
                dispatcher=ScriptedDeliveryDispatcher(),
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            delivery._run_root = delivery_config.artifact_root / "repair-restart"
            delivery._run_root.mkdir(parents=True)
            plan_path = delivery._run_root / "delivery-plan.json"
            plan_path.write_text(json.dumps(_delivery_plan()), encoding="utf-8")
            plan = load_delivery_plan(plan_path)
            git = GitWorkspace(repository, delivery._run_root, plan.slug)
            integration = git.create_integration(git.base_commit())
            finding = ReviewFinding(
                severity=FindingSeverity.MUST_FIX,
                summary="The review finding is still real.",
                evidence="README.md:1",
                source="the accepted repository rule",
            )
            report = ReviewReport(standards=(finding,), spec=())
            repair_commits: list[str] = []

            def write_verdict(*args: object, **kwargs: object) -> None:
                verdict_file = args[3]
                assert isinstance(verdict_file, Path)
                verdict_file.parent.mkdir(parents=True, exist_ok=True)
                verdict_file.write_text(
                    json.dumps(
                        {
                            "accepted": ["standards:1"],
                            "dismissed": [],
                            "rationale": "The citation supports the finding.",
                        }
                    ),
                    encoding="utf-8",
                )

            def repair(*args: object, **kwargs: object) -> DispatchOutcome:
                marker = integration.path / f"repair-{len(repair_commits) + 1}.txt"
                marker.write_text("repaired\n", encoding="utf-8")
                _git(integration.path, "add", marker.name)
                _git(integration.path, "commit", "-m", "fix: repair finding")
                repair_commits.append(_git(integration.path, "rev-parse", "HEAD").stdout.strip())
                return DispatchOutcome("repair", AgentState.DONE, False, "w1:p2")

            review_calls = 0

            def interrupt_after_repair(*args: object, **kwargs: object) -> ReviewReport:
                nonlocal review_calls
                review_calls += 1
                if review_calls == 1:
                    return report
                raise RuntimeError("interrupted after repair")

            with (
                patch.object(
                    delivery,
                    "_review",
                    side_effect=interrupt_after_repair,
                ),
                patch.object(delivery, "_select_worker", return_value=Harness.DROID),
                patch.object(delivery, "_dispatch_artifact", side_effect=write_verdict),
                patch.object(delivery, "_dispatch_with_proxy", side_effect=repair),
                self.assertRaisesRegex(RuntimeError, "interrupted after repair"),
            ):
                delivery._review_and_repair(plan, integration)

            resumed = StandardizedDelivery(
                config,
                dispatcher=ScriptedDeliveryDispatcher(),
                tracker=LocalMarkdownTracker(delivery_config.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            resumed._run_root = delivery._run_root
            resumed_repairs: list[str] = []

            def resumed_repair(*args: object, **kwargs: object) -> DispatchOutcome:
                marker = integration.path / "repair-reset.txt"
                marker.write_text("repaired\n", encoding="utf-8")
                _git(integration.path, "add", marker.name)
                _git(integration.path, "commit", "-m", "fix: reset repair")
                resumed_repairs.append(_git(integration.path, "rev-parse", "HEAD").stdout.strip())
                return DispatchOutcome("repair", AgentState.DONE, False, "w1:p2")

            with (
                patch.object(resumed, "_review", return_value=report),
                patch.object(resumed, "_select_worker", return_value=Harness.DROID),
                patch.object(resumed, "_dispatch_artifact", side_effect=write_verdict),
                patch.object(resumed, "_dispatch_with_proxy", side_effect=resumed_repair),
                self.assertRaisesRegex(DeliveryError, "review_repair_rounds_exhausted"),
            ):
                resumed._review_and_repair(plan, integration)

        self.assertEqual(len(repair_commits), 1)
        self.assertEqual(len(resumed_repairs), 0)

    def test_same_delivery_run_cannot_claim_its_worktrees_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.lock"

            def claim_again() -> None:
                with _delivery_run_claim(path):
                    pass

            with (
                _delivery_run_claim(path),
                self.assertRaisesRegex(
                    DeliveryError,
                    "delivery_run_active",
                ),
            ):
                claim_again()


def _delivery_plan() -> dict[str, object]:
    return {
        "slug": "parallel-delivery",
        "title": "Parallel delivery",
        "problem_statement": "Three slices are missing.",
        "solution": "Add three independent slices.",
        "user_stories": ["As a user, I can observe every slice."],
        "implementation_decisions": ["Keep each slice independent."],
        "testing_decisions": ["Validate each public result."],
        "out_of_scope": [],
        "further_notes": [],
        "seams": ["Repository files"],
        "tickets": [
            {
                "id": identifier,
                "title": f"Implement slice {identifier}",
                "what_to_build": f"Deliver independent slice {identifier}.",
                "blocked_by": [],
                "acceptance_criteria": [f"Slice {identifier} works."],
            }
            for identifier in ("01", "02", "03")
        ],
    }


def _artifact_path(prompt: str) -> Path:
    matches = re.findall(
        r"(?:Write only this UTF-8 JSON file|唯一允许写入的文件)(?::|：)\n([^\n]+)",
        prompt,
    )
    if not matches:
        raise AssertionError(f"artifact path missing from prompt: {prompt[:120]}")
    path = Path(matches[-1].strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _receipt_path(prompt: str) -> Path:
    match = re.search(
        r"write only this additional UTF-8 JSON artifact:\n([^\n]+)",
        prompt,
    )
    if match is None:
        raise AssertionError("receipt path missing")
    return Path(match.group(1).strip())


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


if __name__ == "__main__":
    unittest.main()
