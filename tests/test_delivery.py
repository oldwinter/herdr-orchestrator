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

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.delivery import DeliveryEscalation, StandardizedDelivery
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
            repeated = delivery.run(goal)

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
