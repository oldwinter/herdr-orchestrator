from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_delivery_journal import (
    CompleteDispatcher,
    CrashAfterPlanDispatcher,
    StableTracker,
    _artifact_path,
    _git,
    _initialize_repository,
    _workflow,
)

from herdr_orchestrator.delivery import DeliveryError, StandardizedDelivery
from herdr_orchestrator.delivery_journal import DeliveryJournal
from herdr_orchestrator.delivery_protocol import (
    AuthorityCategory,
    ProxyAction,
    ProxyDecision,
    load_wayfinder_map,
)
from herdr_orchestrator.git_workspace import GitWorkspace
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    WayfinderMode,
)
from herdr_orchestrator.protocol import TransportError


class InspectingDispatcher:
    def __init__(self, state: AgentState) -> None:
        self.state = state
        self.dispatched = 0

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        self.dispatched += 1
        raise AssertionError("repair agent must not be re-dispatched")

    def inspect_agent(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
    ) -> DispatchOutcome | None:
        return DispatchOutcome(name, self.state, True, "w1:p2")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("repair agent should not be read")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("repair agent should not be resumed")


class BlindResponder:
    """A dispatcher without inspect_agent support."""

    def __init__(self) -> None:
        self.responses = 0

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        self.responses += 1
        return DispatchOutcome(name, AgentState.DONE, True, "w1:p2")


class FrontierOverflowDispatcher:
    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        output = _artifact_path(prompt)
        if "Chart a Wayfinder decision map" in prompt:
            payload = {
                "destination": "A specification with every decision fixed.",
                "notes": [],
                "decisions": [
                    {
                        "id": f"{number:02d}",
                        "title": f"Decision {number}",
                        "question": "Which existing component owns this?",
                        "kind": "task",
                        "blocked_by": [],
                        "resolution": "",
                    }
                    for number in range(1, 101)
                ],
                "not_yet_specified": [],
                "out_of_scope": [],
            }
        elif "Resolve exactly one frontier decision" in prompt:
            payload = {
                "ticket_id": "01",
                "resolution": "Reuse the existing store.",
                "new_decisions": [
                    {
                        "id": "101",
                        "title": "Follow-up decision",
                        "question": "Which existing component owns this?",
                        "kind": "task",
                        "blocked_by": ["01"],
                        "resolution": "",
                    }
                ],
                "not_yet_specified": [],
                "out_of_scope": [],
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


class MissingAgentDispatcher(CompleteDispatcher):
    """inspect_agent reports agent_not_found for every agent."""

    def inspect_agent(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
    ) -> DispatchOutcome | None:
        return None


class NotReadyAgentDispatcher(CompleteDispatcher):
    """inspect_agent reports an agent that exists but is not interactive-ready."""

    def inspect_agent(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
    ) -> DispatchOutcome | None:
        raise TransportError("agent_identity_mismatch")


class DeliveryRecoveryGuardTests(unittest.TestCase):
    def test_repair_reconcile_conflicts_while_repair_agent_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            run_root = config.standardized_delivery.artifact_root / "run"
            run_root.mkdir(parents=True)
            git = GitWorkspace(repository, run_root, "delivery")
            base = _git(repository, "rev-parse", "HEAD").stdout.strip()
            integration = git.create_integration(base)
            dispatcher = InspectingDispatcher(AgentState.WORKING)
            delivery = _delivery(config, dispatcher, run_root)
            calls: list[str] = []

            with DeliveryJournal.claim(
                run_root,
                "run",
                60,
                error_type=DeliveryError,
            ) as journal:
                delivery._journal = journal
                with self.assertRaisesRegex(
                    DeliveryError,
                    "delivery_recovery_conflict:repair.commit",
                ):
                    delivery._reconcile_repair_commit(
                        git,
                        integration,
                        1,
                        base,
                        dispatch=lambda: calls.append("dispatch"),
                        harness=Harness.DROID,
                    )

            self.assertEqual(calls, [])
            self.assertEqual(dispatcher.dispatched, 0)

    def test_repair_reconcile_dispatches_once_the_repair_agent_settles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            run_root = config.standardized_delivery.artifact_root / "run"
            run_root.mkdir(parents=True)
            git = GitWorkspace(repository, run_root, "delivery")
            base = _git(repository, "rev-parse", "HEAD").stdout.strip()
            integration = git.create_integration(base)
            dispatcher = InspectingDispatcher(AgentState.DONE)
            delivery = _delivery(config, dispatcher, run_root)
            receipt = run_root / "repairs" / "round-1.json"

            def dispatch() -> None:
                (integration.path / "repair.txt").write_text(
                    "repaired\n",
                    encoding="utf-8",
                )
                _git(integration.path, "add", "repair.txt")
                _git(integration.path, "commit", "-m", "fix: repair accepted finding")
                commit = _git(integration.path, "rev-parse", "HEAD").stdout.strip()
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text(
                    json.dumps(
                        {"round": 1, "before_commit": base, "commit": commit}
                    ),
                    encoding="utf-8",
                )

            with DeliveryJournal.claim(
                run_root,
                "run",
                60,
                error_type=DeliveryError,
            ) as journal:
                delivery._journal = journal
                commit = delivery._reconcile_repair_commit(
                    git,
                    integration,
                    1,
                    base,
                    dispatch=dispatch,
                    harness=Harness.DROID,
                )

            self.assertEqual(commit, git.head(integration))
            self.assertNotEqual(commit, base)

    def test_uninspectable_started_proxy_response_is_a_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            run_root = config.standardized_delivery.artifact_root / "run"
            workspace = run_root / "worktrees" / "ticket-01"
            workspace.mkdir(parents=True)
            dispatcher = BlindResponder()
            delivery = _delivery(config, dispatcher, run_root)
            decision_file = run_root / "proxy" / "worker-1.json"
            decision_file.parent.mkdir(parents=True)
            decision_file.write_text(
                json.dumps(
                    {
                        "action": "answer",
                        "category": "spec-authorized",
                        "response": "Use the accepted default.",
                        "rationale": "The specification fixes the choice.",
                    }
                ),
                encoding="utf-8",
            )
            decision = ProxyDecision(
                action=ProxyAction.ANSWER,
                category=AuthorityCategory.SPEC_AUTHORIZED,
                response="Use the accepted default.",
                rationale="The specification fixes the choice.",
            )
            persist = DeliveryJournal._persist_event
            interrupted = [False]

            def interrupt(self, event, key, kind, details, observed_at):
                if (
                    not interrupted[0]
                    and event == "effect_confirmed"
                    and isinstance(key, str)
                    and key.startswith("agent:response:")
                ):
                    interrupted[0] = True
                    raise RuntimeError("process died after proxy response")
                persist(self, event, key, kind, details, observed_at)

            with DeliveryJournal.claim(
                run_root,
                "run",
                60,
                error_type=DeliveryError,
            ) as journal:
                delivery._journal = journal
                with (
                    patch.object(DeliveryJournal, "_persist_event", interrupt),
                    self.assertRaisesRegex(RuntimeError, "process died"),
                ):
                    delivery._respond_with_journal(
                        workspace,
                        "hd-worker-1",
                        Harness.DROID,
                        decision,
                        question_hash="question-1",
                        proxy_round=1,
                        decision_file=decision_file,
                    )
                self.assertTrue(interrupted[0])
                with self.assertRaisesRegex(
                    DeliveryError,
                    "delivery_recovery_conflict:agent.respond",
                ):
                    delivery._respond_with_journal(
                        workspace,
                        "hd-worker-1",
                        Harness.DROID,
                        decision,
                        question_hash="question-1",
                        proxy_round=1,
                        decision_file=decision_file,
                    )

            self.assertEqual(dispatcher.responses, 1)

    def test_publish_rejects_source_workspace_drift(self) -> None:
        mutations = (
            ("rewrite", "delivery_source_workspace_drifted"),
            ("dirty", "delivery_source_workspace_dirty"),
        )
        for mutation, expected_error in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary).resolve() / "repository"
                _initialize_repository(repository)
                config = _workflow(repository)
                goal = repository / "goal.md"
                goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
                delivery = StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=StableTracker(),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                )
                review = delivery._review_and_repair

                def drift_after_review(plan, integration):
                    rounds = review(plan, integration)
                    if mutation == "rewrite":
                        (repository / "drift.txt").write_text(
                            "drifted\n", encoding="utf-8"
                        )
                        _git(repository, "add", "drift.txt")
                        _git(
                            repository,
                            "commit",
                            "--amend",
                            "-m",
                            "chore: rewritten history",
                        )
                    else:
                        with (repository / "README.md").open("a", encoding="utf-8") as handle:
                            handle.write("dirty\n")
                    return rounds

                with (
                    patch.object(
                        delivery,
                        "_review_and_repair",
                        side_effect=drift_after_review,
                    ),
                    self.assertRaisesRegex(DeliveryError, expected_error),
                ):
                    delivery.run(goal)

    def test_completed_result_rejects_source_workspace_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            result = StandardizedDelivery(
                config,
                dispatcher=CompleteDispatcher(),
                tracker=StableTracker(external),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            ).run(goal)
            self.assertEqual(result.status, "succeeded")
            (repository / "drift.txt").write_text("drifted\n", encoding="utf-8")
            _git(repository, "add", "drift.txt")
            _git(repository, "commit", "--amend", "-m", "chore: rewritten history")

            with self.assertRaisesRegex(
                DeliveryError,
                "delivery_source_workspace_drifted",
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=CompleteDispatcher(),
                    tracker=StableTracker(external),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)

    def test_artifact_recovery_matches_when_agent_inspection_is_inconclusive(self) -> None:
        for dispatcher_type in (MissingAgentDispatcher, NotReadyAgentDispatcher):
            with self.subTest(dispatcher=dispatcher_type.__name__):
                with tempfile.TemporaryDirectory() as temporary:
                    repository = Path(temporary).resolve() / "repository"
                    _initialize_repository(repository)
                    config = _workflow(repository)
                    goal = repository / "goal.md"
                    goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
                    crashed_dispatcher = CrashAfterPlanDispatcher()
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "controller died after plan artifact",
                    ):
                        StandardizedDelivery(
                            config,
                            dispatcher=crashed_dispatcher,
                            tracker=StableTracker(),
                            controller_harness=Harness.DROID,
                            worker_harnesses=(Harness.DROID,),
                        ).run(goal)

                    resumed_dispatcher = dispatcher_type()
                    result = StandardizedDelivery(
                        config,
                        dispatcher=resumed_dispatcher,
                        tracker=StableTracker(),
                        controller_harness=Harness.DROID,
                        worker_harnesses=(Harness.DROID,),
                    ).run(goal)

                    self.assertEqual(result.status, "succeeded")
                    self.assertEqual(crashed_dispatcher.calls, 1)
                    self.assertFalse(
                        any(
                            "Create one accepted specification" in prompt
                            for prompt in resumed_dispatcher.prompts
                        )
                    )

    def test_wayfinder_refuses_to_write_a_map_over_the_decision_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            config = replace(
                config,
                standardized_delivery=replace(
                    config.standardized_delivery,
                    wayfinder=WayfinderMode.ALWAYS,
                ),
            )
            run_root = config.standardized_delivery.artifact_root / "run"
            run_root.mkdir(parents=True)
            delivery = _delivery(config, FrontierOverflowDispatcher(), run_root)
            delivery._goal = "Deliver a recoverable slice."
            map_path = run_root / "wayfinder-map.json"

            with self.assertRaisesRegex(DeliveryError, "wayfinder_decision_limit"):
                delivery._run_wayfinder()

            self.assertEqual(len(load_wayfinder_map(map_path).decisions), 100)


def _delivery(
    config,
    dispatcher: object,
    run_root: Path,
) -> StandardizedDelivery:
    delivery = StandardizedDelivery.__new__(StandardizedDelivery)
    delivery.config = config
    delivery.dispatcher = dispatcher
    delivery.tracker = None
    delivery.controller = Harness.DROID
    delivery.controller_override = Harness.DROID
    delivery.controller_auto = False
    delivery.worker_harnesses = (Harness.DROID,)
    delivery.health = None
    delivery.readiness_probe = None
    delivery._health_harnesses = (Harness.DROID,)
    delivery._health_snapshot = None
    delivery._goal = ""
    delivery._run_id = "run"
    delivery._run_root = run_root
    delivery._ledger_lock = threading.Lock()
    delivery._previous_state = {}
    delivery._journal = None
    delivery._legacy_reconstruction_read_only = False
    delivery._lease_seconds = None
    return delivery


if __name__ == "__main__":
    unittest.main()
