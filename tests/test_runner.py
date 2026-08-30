from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.herdr import replica_slot_names, worktree_agent_name
from herdr_orchestrator.model import (
    AgentState,
    DispatchContext,
    DispatchOutcome,
    Harness,
    NewJob,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
)
from herdr_orchestrator.readiness import HarnessHealthRegistry
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeDispatcher:
    def __init__(
        self,
        outcomes: dict[Harness, DispatchOutcome],
        *,
        route_output: Path | None = None,
        routed_harness: Harness | None = None,
        topology_placement: str | None = None,
        delay_seconds: float = 0,
        planner_output: Path | None = None,
        planned_harness: Harness | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.route_output = route_output
        self.routed_harness = routed_harness
        self.topology_placement = topology_placement
        self.delay_seconds = delay_seconds
        self.planner_output = planner_output
        self.planned_harness = planned_harness
        self.calls: list[Harness] = []
        self.timeouts: list[float] = []
        self.prompts: dict[Harness, str] = {}
        self.prompt_history: list[str] = []
        self.contexts: list[DispatchContext | None] = []
        self.closed_created_agents: list[str] = []

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        self.timeouts.append(timeout_seconds)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        self.calls.append(harness)
        self.prompts[harness] = prompt
        self.prompt_history.append(prompt)
        self.contexts.append(context)
        if self.route_output is not None and self.routed_harness is not None:
            self.route_output.write_text(
                json.dumps({"harness": self.routed_harness.value}),
                encoding="utf-8",
            )
        if self.topology_placement is not None and "Choose the Herdr execution topology" in prompt:
            match = re.search(
                r"Write only this UTF-8 JSON file:\n([^\n]+)",
                prompt,
            )
            if match is None:
                raise AssertionError("topology output path missing")
            output = Path(match.group(1).strip())
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "placement": self.topology_placement,
                        "rationale": "Use the requested test topology.",
                    }
                ),
                encoding="utf-8",
            )
        if (
            self.planner_output is not None
            and self.planned_harness is not None
            and '"tasks"' in prompt
        ):
            self.planner_output.parent.mkdir(parents=True, exist_ok=True)
            self.planner_output.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "title": "Planned",
                                "harness": self.planned_harness.value,
                                "prompt": "Inspect the repository.",
                                "dedupe_key": "planned-ready-worker",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
        return self.outcomes[harness]

    def close_created_agent(self, name: str) -> None:
        self.closed_created_agents.append(name)


class CleanupDispatcher(FakeDispatcher):
    def __init__(self) -> None:
        super().__init__({})
        self.closed: list[tuple[str, PlacementTarget, str]] = []

    def close_agent_terminal(
        self,
        name: str,
        placement: PlacementTarget,
        *,
        expected_pane_id: str,
        dry_run: bool,
    ) -> dict[str, object]:
        if not dry_run:
            self.closed.append((name, placement, expected_pane_id))
        return {
            "agent_name": name,
            "placement": placement.value,
            "pane_id": expected_pane_id,
            "action": "would_close" if dry_run else "closed",
        }


class ResumeDispatcher(FakeDispatcher):
    def __init__(
        self,
        outcomes: dict[Harness, DispatchOutcome],
        resume_outcome: DispatchOutcome,
    ) -> None:
        super().__init__(outcomes)
        self.resume_outcome = resume_outcome
        self.responses: list[tuple[str, Harness, str, int, str, DispatchContext | None]] = []

    def respond(
        self,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
        expected_pane_id: str,
        context: DispatchContext | None,
    ) -> DispatchOutcome:
        self.responses.append(
            (
                name,
                harness,
                response,
                timeout_seconds,
                expected_pane_id,
                context,
            )
        )
        return self.resume_outcome


class CoordinatorTests(unittest.TestCase):
    def test_auto_routing_uses_ready_controller_and_worker_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(
                base,
                state_db=root / "state.db",
                planner=replace(base.planner, output_file=root / "plans/planner.json"),
            )
            prompt_file = root / "task.md"
            prompt_file.write_text("Implement a focused change.", encoding="utf-8")
            route_output = root / "plans/route-1ab25567ce25.json"
            dispatcher = FakeDispatcher(
                {
                    Harness.GROK: DispatchOutcome(
                        "router",
                        AgentState.DONE,
                        False,
                        "w1:p2",
                    )
                },
                route_output=route_output,
                routed_harness=Harness.GROK,
            )
            store = Store(config.state_db)
            health = HarnessHealthRegistry(
                config,
                store,
                probe=lambda workflow, harness, timeout: (
                    {
                        "status": "ready",
                        "error_code": None,
                        "error_summary": None,
                    }
                    if harness is Harness.GROK
                    else {
                        "status": "timeout",
                        "error_code": "agent_turn_not_observed",
                        "error_summary": None,
                    }
                ),
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )
            observability = MagicMock()
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                worker_harnesses=(Harness.DROID, Harness.GROK),
                observability=observability,
                health_registry=health,
            )

            _, created, selected = coordinator.enqueue_prompt_file(
                harness=None,
                title="Build",
                prompt_file=prompt_file,
                dedupe_key="ready-route",
            )

        self.assertTrue(created)
        self.assertEqual(selected, Harness.GROK)
        self.assertEqual(dispatcher.calls, [Harness.GROK])
        self.assertIn('"harness": "grok"', dispatcher.prompts[Harness.GROK])
        self.assertNotIn('"harness": "droid"', dispatcher.prompts[Harness.GROK])
        event_names = [call.args[0] for call in observability.event.call_args_list]
        self.assertIn("harness_candidates_evaluated", event_names)
        self.assertEqual(event_names.count("harness_selected"), 2)

    def test_pending_job_waits_for_ready_worker_without_consuming_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            job_id, _ = store.enqueue(_job(config.name, Harness.DROID))
            now = [100.0]
            ready = [False]
            health = HarnessHealthRegistry(
                config,
                store,
                probe=lambda workflow, harness, timeout: {
                    "status": "ready" if ready[0] else "timeout",
                    "error_code": None if ready[0] else "herdr_timeout",
                    "error_summary": None,
                },
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: now[0],
            )
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "droid-worker",
                        AgentState.DONE,
                        False,
                        "w1:p2",
                    )
                }
            )
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                worker_harnesses=(Harness.DROID,),
                health_registry=health,
            )

            deferred = coordinator.run_once()
            job_before_recovery = store.jobs(config.name)[0]
            ready[0] = True
            now[0] = 401.0
            recovered = coordinator.run_once()

        self.assertEqual(deferred["claimed"], 0)
        self.assertEqual(job_before_recovery["id"], job_id)
        self.assertEqual(job_before_recovery["state"], "pending")
        self.assertEqual(job_before_recovery["attempts"], 0)
        self.assertEqual(deferred["harness_health"][0]["status"], "degraded")
        self.assertEqual(recovered["succeeded"], 1)
        self.assertEqual(dispatcher.calls, [Harness.DROID])

    def test_explicit_unhealthy_worker_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
            )
            prompt_file = root / "task.md"
            prompt_file.write_text("Implement a focused change.", encoding="utf-8")
            dispatcher = FakeDispatcher({})
            health = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                probe=lambda workflow, harness, timeout: (
                    {
                        "status": "ready",
                        "error_code": None,
                        "error_summary": None,
                    }
                    if harness is Harness.GROK
                    else {
                        "status": "auth_required",
                        "error_code": "agent_auth_required",
                        "error_summary": None,
                    }
                ),
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )
            coordinator = Coordinator(
                config,
                dispatcher=dispatcher,
                worker_harnesses=(Harness.DROID, Harness.GROK),
                health_registry=health,
            )

            with self.assertRaisesRegex(
                ValueError,
                "harness_unavailable:droid:agent_auth_required",
            ):
                coordinator.enqueue_prompt_file(
                    harness=Harness.DROID,
                    title="Build",
                    prompt_file=prompt_file,
                    dedupe_key="explicit-unhealthy-worker",
                )

        self.assertEqual(dispatcher.calls, [])

    def test_automatic_all_unavailable_selection_reports_each_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
            )
            prompt_file = root / "task.md"
            prompt_file.write_text("Implement a focused change.", encoding="utf-8")
            dispatcher = FakeDispatcher({})
            health = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                probe=lambda workflow, harness, timeout: (
                    {
                        "status": "auth_required",
                        "error_code": "agent_auth_required",
                        "error_summary": None,
                    }
                    if harness is Harness.DROID
                    else {
                        "status": "model_invalid",
                        "error_code": "agent_model_invalid",
                        "error_summary": None,
                    }
                ),
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )
            coordinator = Coordinator(
                config,
                dispatcher=dispatcher,
                worker_harnesses=(Harness.DROID, Harness.GROK),
                health_registry=health,
            )

            with self.assertRaisesRegex(
                ValueError,
                "controller_harness_unavailable:"
                "droid=agent_auth_required,grok=agent_model_invalid",
            ):
                coordinator.enqueue_prompt_file(
                    harness=None,
                    title="Build",
                    prompt_file=prompt_file,
                    dedupe_key="all-unavailable",
                )

        self.assertEqual(dispatcher.calls, [])

    def test_planner_catalog_contains_only_ready_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            planner_output = root / "plans/planner.json"
            config = replace(
                base,
                state_db=root / "state.db",
                planner=replace(
                    base.planner,
                    enabled=True,
                    interval_seconds=0,
                    output_file=planner_output,
                    worker_harnesses=(Harness.DROID, Harness.GROK),
                ),
            )
            dispatcher = FakeDispatcher(
                {
                    Harness.GROK: DispatchOutcome(
                        "planner",
                        AgentState.DONE,
                        True,
                        "w1:p1",
                    )
                },
                planner_output=planner_output,
                planned_harness=Harness.GROK,
            )
            store = Store(config.state_db)
            health = HarnessHealthRegistry(
                config,
                store,
                probe=lambda workflow, harness, timeout: (
                    {
                        "status": "ready",
                        "error_code": None,
                        "error_summary": None,
                    }
                    if harness is Harness.GROK
                    else {
                        "status": "timeout",
                        "error_code": "herdr_timeout",
                        "error_summary": None,
                    }
                ),
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                worker_harnesses=(Harness.DROID, Harness.GROK),
                health_registry=health,
            )
            coordinator.initialize()

            coordinator._run_planner_if_due()
            planned_jobs = store.jobs(config.name)

        self.assertEqual(planned_jobs[0]["harness"], "grok")
        self.assertIn('"harness":"grok"', dispatcher.prompt_history[0])
        self.assertNotIn('"harness":"droid', dispatcher.prompt_history[0])

    def test_explicit_unhealthy_controller_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(
                base,
                state_db=root / "state.db",
                planner=replace(base.planner, output_file=root / "plans/planner.json"),
            )
            prompt_file = root / "task.md"
            prompt_file.write_text("Implement a focused change.", encoding="utf-8")
            dispatcher = FakeDispatcher({})
            store = Store(config.state_db)
            health = HarnessHealthRegistry(
                config,
                store,
                probe=lambda workflow, harness, timeout: (
                    {
                        "status": "ready",
                        "error_code": None,
                        "error_summary": None,
                    }
                    if harness is Harness.GROK
                    else {
                        "status": "auth_required",
                        "error_code": "agent_auth_required",
                        "error_summary": None,
                    }
                ),
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID, Harness.GROK),
                health_registry=health,
            )

            with self.assertRaisesRegex(
                ValueError,
                "harness_unavailable:droid:agent_auth_required",
            ):
                coordinator.enqueue_prompt_file(
                    harness=None,
                    title="Build",
                    prompt_file=prompt_file,
                    dedupe_key="explicit-unhealthy-controller",
                )

        self.assertEqual(dispatcher.calls, [])

    def test_run_until_idle_reports_unavailable_worker_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            health = HarnessHealthRegistry(
                config,
                store,
                probe=lambda workflow, harness, timeout: {
                    "status": "model_invalid",
                    "error_code": "agent_model_invalid",
                    "error_summary": None,
                },
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )

            result = Coordinator(
                config,
                store=store,
                dispatcher=FakeDispatcher({}),
                worker_harnesses=(Harness.DROID,),
                health_registry=health,
            ).run_until_idle(timeout_seconds=10)

        self.assertFalse(result["idle"])
        self.assertEqual(result["reason"], "worker_pool_unavailable")
        self.assertFalse(result["worker_pool_idle"])
        self.assertFalse(result["queue_idle"])
        self.assertEqual(result["queue"]["pending"], 1)
        self.assertEqual(
            result["harness_health"][0]["reason_code"],
            "agent_model_invalid",
        )

    def test_planner_drain_reports_unavailable_worker_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            droid_worker = next(
                worker for worker in base.workers if worker.harness is Harness.DROID
            )
            config = replace(
                base,
                state_db=root / "state.db",
                workers=(droid_worker,),
                planner=replace(
                    base.planner,
                    enabled=True,
                    interval_seconds=0,
                    output_file=root / "plans/planner.json",
                    worker_harnesses=(Harness.DROID,),
                ),
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(
                replace(
                    _job(config.name, Harness.DROID),
                    placement=PlacementTarget.PANE,
                )
            )
            health = HarnessHealthRegistry(
                config,
                store,
                probe=lambda workflow, harness, timeout: {
                    "status": "timeout",
                    "error_code": "herdr_timeout",
                    "error_summary": None,
                },
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )

            result = Coordinator(
                config,
                store=store,
                dispatcher=FakeDispatcher({}),
                worker_harnesses=(Harness.DROID,),
                health_registry=health,
            ).run_until_idle(timeout_seconds=10)

        self.assertFalse(result["idle"])
        self.assertEqual(result["reason"], "worker_pool_unavailable")
        self.assertEqual(result["claimed"], 0)
        self.assertEqual(result["queue"]["pending"], 1)
        self.assertEqual(
            result["harness_health"][0]["reason_code"],
            "herdr_timeout",
        )

    def test_dispatches_claimed_jobs_and_records_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            store.enqueue(_job(config.name, Harness.CLAUDE))
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "droid-worker",
                        AgentState.DONE,
                        False,
                        "w1:p2",
                    ),
                    Harness.CLAUDE: DispatchOutcome(
                        "claude-worker",
                        AgentState.BLOCKED,
                        False,
                        "w1:p3",
                    ),
                }
            )
            coordinator = Coordinator(config, store=store, dispatcher=dispatcher)

            result = coordinator.run_once()

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["claimed"], 2)
        self.assertEqual(result["batch"]["succeeded"], 1)
        self.assertEqual(result["queue"]["succeeded"], 1)
        self.assertEqual(result["queue"]["blocked"], 1)
        self.assertCountEqual(dispatcher.calls, [Harness.DROID, Harness.CLAUDE])
        self.assertIn("# Factory Droid execution profile", dispatcher.prompts[Harness.DROID])
        self.assertIn("# Claude Code execution profile", dispatcher.prompts[Harness.CLAUDE])
        self.assertIn("# Task packet\n\nRead only.", dispatcher.prompts[Harness.DROID])
        self.assertNotIn("Hermes Agent execution profile", dispatcher.prompts[Harness.DROID])

    def test_run_until_idle_drains_replica_limited_jobs_across_waves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            for index in range(3):
                store.enqueue(_job(config.name, Harness.DROID, suffix=str(index)))
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "droid-worker",
                        AgentState.DONE,
                        False,
                        "w1:p2",
                    )
                }
            )
            coordinator = Coordinator(config, store=store, dispatcher=dispatcher)

            result = coordinator.run_until_idle(timeout_seconds=10)

        self.assertTrue(result["idle"])
        self.assertEqual(result["waves"], 3)
        self.assertEqual(result["claimed"], 3)
        self.assertEqual(result["batch"]["succeeded"], 3)
        self.assertEqual(result["queue"]["succeeded"], 3)
        self.assertEqual(result["queue"]["pending"], 0)
        self.assertTrue(result["queue_idle"])
        self.assertTrue(result["worker_pool_idle"])
        self.assertTrue(all(0 < timeout <= 10 for timeout in dispatcher.timeouts))

    def test_run_until_idle_reports_blocked_instead_of_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "worker",
                        AgentState.BLOCKED,
                        False,
                        "w1:p2",
                        "agent_blocked",
                    )
                }
            )

            result = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
            ).run_until_idle(timeout_seconds=10)

        self.assertFalse(result["idle"])
        self.assertEqual(result["reason"], "blocked")
        self.assertFalse(result["worker_pool_idle"])
        self.assertFalse(result["queue_idle"])
        self.assertEqual(result["queue"]["blocked"], 1)

    def test_resume_blocked_uses_same_agent_pane_and_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            dispatcher = ResumeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "droid-worker",
                        AgentState.BLOCKED,
                        False,
                        "w1:p2",
                        "agent_blocked",
                    )
                },
                DispatchOutcome(
                    "droid-worker",
                    AgentState.DONE,
                    True,
                    "w1:p2",
                ),
            )
            health = HarnessHealthRegistry(
                config,
                store,
                probe=lambda workflow, harness, timeout: {
                    "status": "ready",
                    "error_code": None,
                    "error_summary": None,
                },
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                health_registry=health,
            )
            job_id, _ = store.enqueue(_job(config.name, Harness.DROID))
            coordinator.run_once()
            health.record_probe(
                Harness.DROID,
                {
                    "status": "timeout",
                    "error_code": "herdr_timeout",
                    "error_summary": None,
                },
            )

            result = coordinator.resume_blocked(job_id, "Approve this local action.")
            job = store.jobs(config.name)[0]
            recovered_health = health.projection((Harness.DROID,))[0]

        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(dispatcher.calls, [Harness.DROID])
        self.assertEqual(len(dispatcher.responses), 1)
        response = dispatcher.responses[0]
        self.assertEqual(response[0], "droid-worker")
        self.assertEqual(response[2], "Approve this local action.")
        self.assertEqual(response[4], "w1:p2")
        self.assertEqual(recovered_health["status"], "ready")

    def test_worker_dispatch_health_uses_the_execution_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            execution_path = root / ".orchestrator/worktrees/task"
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            health = HarnessHealthRegistry(
                config,
                store,
                probe=lambda workflow, harness, timeout: {
                    "status": "ready",
                    "error_code": None,
                    "error_summary": None,
                },
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "droid-worker",
                        AgentState.UNKNOWN,
                        False,
                        "w1:p2",
                        "agent_auth_required",
                        execution_path=str(execution_path),
                    )
                }
            )

            Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                worker_harnesses=(Harness.DROID,),
                health_registry=health,
            ).run_once()
            root_health = health.projection((Harness.DROID,))[0]
            execution_health = health.projection(
                (Harness.DROID,),
                workspace=execution_path,
            )[0]

        self.assertEqual(root_health["status"], "ready")
        self.assertEqual(execution_health["status"], "unavailable")
        self.assertEqual(
            execution_health["reason_code"],
            "agent_auth_required",
        )

    def test_run_until_idle_does_not_report_idle_after_the_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "droid-worker",
                        AgentState.DONE,
                        False,
                        "w1:p2",
                    )
                },
                delay_seconds=1.01,
            )

            result = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
            ).run_until_idle(timeout_seconds=1)

        self.assertFalse(result["idle"])
        self.assertEqual(result["reason"], "drain_timeout")
        self.assertTrue(result["queue_idle"])
        self.assertLessEqual(dispatcher.timeouts[0], 1)

    def test_run_until_idle_deadline_bounds_topology_before_worker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(
                base,
                state_db=root / "state.db",
                planner=replace(base.planner, output_file=root / "plans/planner.json"),
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow=config.name,
                    title="Ambiguous",
                    harness=Harness.GROK,
                    prompt="Determine the best execution approach.",
                    dedupe_key="drain-topology-deadline",
                    max_attempts=2,
                    placement=None,
                )
            )
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome("controller", AgentState.DONE, True, "w1:p1"),
                    Harness.GROK: DispatchOutcome("worker", AgentState.DONE, False, "w1:p2"),
                },
                topology_placement="pane",
                delay_seconds=1.01,
            )
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                controller_harness=Harness.DROID,
            )

            result = coordinator.run_until_idle(timeout_seconds=1)

        self.assertFalse(result["idle"])
        self.assertEqual(result["reason"], "drain_timeout")
        self.assertEqual(dispatcher.calls, [Harness.DROID])
        self.assertLessEqual(dispatcher.timeouts[0], 1)
        self.assertEqual(result["queue"]["pending"], 1)

    def test_run_until_idle_deadline_bounds_planner_before_worker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(
                base,
                state_db=root / "state.db",
                planner=replace(
                    base.planner,
                    enabled=True,
                    interval_seconds=0,
                    output_file=root / "plans/planner.json",
                ),
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(
                replace(
                    _job(config.name, Harness.DROID),
                    placement=PlacementTarget.PANE,
                )
            )
            dispatcher = FakeDispatcher(
                {Harness.DROID: DispatchOutcome("controller", AgentState.DONE, True, "w1:p1")},
                delay_seconds=1.01,
            )
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                controller_harness=Harness.DROID,
            )

            result = coordinator.run_until_idle(timeout_seconds=1)

        self.assertFalse(result["idle"])
        self.assertEqual(result["reason"], "drain_timeout")
        self.assertEqual(dispatcher.calls, [Harness.DROID])
        self.assertLessEqual(dispatcher.timeouts[0], 1)
        self.assertEqual(result["queue"]["pending"], 1)

    def test_run_until_idle_distinguishes_worker_pool_from_global_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID, suffix="selected"))
            store.enqueue(_job(config.name, Harness.CLAUDE, suffix="excluded"))
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "droid-worker",
                        AgentState.DONE,
                        False,
                        "w1:p2",
                    )
                }
            )
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                worker_harnesses=(Harness.DROID,),
            )

            result = coordinator.run_until_idle(timeout_seconds=10)

        self.assertTrue(result["idle"])
        self.assertTrue(result["worker_pool_idle"])
        self.assertFalse(result["queue_idle"])
        self.assertEqual(result["reason"], "worker_pool_idle")
        self.assertEqual(result["queue"]["pending"], 1)

    def test_run_until_idle_initializes_a_fresh_empty_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            coordinator = Coordinator(config, dispatcher=FakeDispatcher({}))

            result = coordinator.run_until_idle(timeout_seconds=10)

        self.assertTrue(result["idle"])
        self.assertEqual(result["claimed"], 0)
        self.assertEqual(result["queue"]["pending"], 0)

    def test_gc_closes_only_owned_succeeded_non_worktree_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            tab_id, _ = store.enqueue(
                NewJob(
                    workflow=config.name,
                    title="tab",
                    harness=Harness.DROID,
                    prompt="Read only.",
                    dedupe_key="gc-tab",
                    max_attempts=1,
                    placement=PlacementTarget.TAB,
                )
            )
            worktree_id, _ = store.enqueue(
                NewJob(
                    workflow=config.name,
                    title="worktree",
                    harness=Harness.GROK,
                    prompt="Write a file.",
                    dedupe_key="gc-worktree",
                    max_attempts=1,
                    placement=PlacementTarget.WORKTREE,
                )
            )
            foreign_id, _ = store.enqueue(
                NewJob(
                    workflow=config.name,
                    title="foreign",
                    harness=Harness.CLAUDE,
                    prompt="Read only.",
                    dedupe_key="gc-foreign",
                    max_attempts=1,
                    placement=PlacementTarget.TAB,
                )
            )
            tab_name = replica_slot_names(
                config.name,
                config.workspace,
                Harness.DROID,
                1,
                PlacementTarget.TAB,
            )[0]
            worktree_name = worktree_agent_name(config.name, Harness.GROK, worktree_id)
            claimed = store.claim(
                config.name,
                limit=3,
                lease_seconds=60,
                slot_names={
                    Harness.DROID.value: (tab_name,),
                    f"{Harness.GROK.value}:worktree:{worktree_id}": (worktree_name,),
                    Harness.CLAUDE.value: ("foreign-agent",),
                },
            )
            for job in claimed:
                store.record_outcome(
                    job,
                    DispatchOutcome(
                        job.agent_name,
                        AgentState.DONE,
                        False,
                        "w1:p2",
                        placement=job.placement,
                    ),
                )
            dispatcher = CleanupDispatcher()
            coordinator = Coordinator(config, store=store, dispatcher=dispatcher)

            preview = coordinator.gc_succeeded_agents(dry_run=True)
            applied = coordinator.gc_succeeded_agents(dry_run=False)

        self.assertEqual(tab_id, preview["candidates"][0]["job_id"])
        self.assertEqual(preview["candidate_count"], 1)
        self.assertEqual(preview["skipped_worktrees"], 1)
        self.assertEqual(preview["skipped_unowned"], 1)
        self.assertEqual(preview["actions"][0]["action"], "would_close")
        self.assertEqual(applied["actions"][0]["action"], "closed")
        self.assertEqual(
            dispatcher.closed,
            [(tab_name, PlacementTarget.TAB, "w1:p2")],
        )

    def test_gc_skips_a_preexisting_reused_deterministic_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            name = replica_slot_names(
                config.name,
                config.workspace,
                Harness.DROID,
                1,
            )[0]
            claimed = store.claim(
                config.name,
                limit=1,
                lease_seconds=60,
                slot_names={Harness.DROID.value: (name,)},
            )[0]
            store.record_outcome(
                claimed,
                DispatchOutcome(
                    name,
                    AgentState.DONE,
                    True,
                    "w1:p9",
                    placement=PlacementTarget.TAB,
                ),
            )
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=CleanupDispatcher(),
            )

            result = coordinator.gc_succeeded_agents(dry_run=False)

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["skipped_unowned"], 1)

    def test_gc_can_preview_owned_failed_agents_but_excludes_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(
                replace(
                    _job(config.name, Harness.DROID, suffix="failed"),
                    max_attempts=1,
                )
            )
            store.enqueue(_job(config.name, Harness.CLAUDE, suffix="blocked"))
            droid_name = replica_slot_names(
                config.name,
                config.workspace,
                Harness.DROID,
                1,
            )[0]
            claude_name = replica_slot_names(
                config.name,
                config.workspace,
                Harness.CLAUDE,
                1,
            )[0]
            claimed = store.claim(
                config.name,
                limit=2,
                lease_seconds=60,
                slot_names={
                    Harness.DROID.value: (droid_name,),
                    Harness.CLAUDE.value: (claude_name,),
                },
            )
            for job in claimed:
                if job.harness is Harness.DROID:
                    outcome = DispatchOutcome(
                        job.agent_name,
                        AgentState.UNKNOWN,
                        False,
                        "w1:p2",
                        "agent_provider_failed",
                    )
                else:
                    outcome = DispatchOutcome(
                        job.agent_name,
                        AgentState.BLOCKED,
                        False,
                        "w1:p3",
                        "agent_blocked",
                    )
                store.record_outcome(job, outcome)
            dispatcher = CleanupDispatcher()
            coordinator = Coordinator(config, store=store, dispatcher=dispatcher)

            result = coordinator.gc_failed_agents(dry_run=True)

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["candidates"][0]["state"], "failed")
        self.assertEqual(result["candidates"][0]["agent_name"], droid_name)
        self.assertEqual(result["skipped_blocked"], 1)
        self.assertEqual(dispatcher.closed, [])

    def test_enqueue_carries_declared_receipt_through_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
            )
            prompt_file = root / "task.md"
            prompt_file.write_text("Read only. Inspect README.", encoding="utf-8")
            receipt = TaskReceipt(ReceiptKind.OUTPUT_PREFIX, "MOCK-OK harness=pi")
            dispatcher = FakeDispatcher(
                {
                    Harness.PI: DispatchOutcome(
                        "pi-worker",
                        AgentState.DONE,
                        False,
                        "w1:p2",
                        task_verified=True,
                    )
                }
            )
            coordinator = Coordinator(config, dispatcher=dispatcher)

            coordinator.enqueue_prompt_file(
                harness=Harness.PI,
                title="inspect",
                prompt_file=prompt_file,
                dedupe_key="inspect-receipt-v1",
                receipt=receipt,
            )
            coordinator.run_once()
            job = coordinator.store.jobs(config.name)[0]

        self.assertEqual(dispatcher.contexts[0].receipt, receipt)
        self.assertIs(job["agent_settled"], True)
        self.assertIs(job["task_verified"], True)

    def test_seed_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            coordinator = Coordinator(config, dispatcher=FakeDispatcher({}))

            first = coordinator.seed()
            second = coordinator.seed()

        self.assertEqual(first, (6, 0))
        self.assertEqual(second, (0, 6))

    def test_auto_enqueue_uses_controller_to_select_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
                planner=replace(
                    load_workflow(REPO_ROOT / "workflows/multi-harness.toml").planner,
                    output_file=root / "plans/planner.json",
                ),
            )
            prompt_file = root / "task.md"
            prompt_file.write_text("Implement a focused change.", encoding="utf-8")
            route_output = root / "plans/route-1446f8ee80d5.json"
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "router",
                        AgentState.DONE,
                        False,
                        "w1:p2",
                    )
                },
                route_output=route_output,
                routed_harness=Harness.GROK,
            )
            coordinator = Coordinator(
                config,
                dispatcher=dispatcher,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.GROK, Harness.CODEX),
            )

            job_id, created, selected = coordinator.enqueue_prompt_file(
                harness=None,
                title="Build",
                prompt_file=prompt_file,
                dedupe_key="auto-route",
            )

            jobs = coordinator.store.jobs(config.name)
            repeated = coordinator.enqueue_prompt_file(
                harness=None,
                title="Build again",
                prompt_file=prompt_file,
                dedupe_key="auto-route",
            )

        self.assertTrue(created)
        self.assertEqual(job_id, jobs[0]["id"])
        self.assertEqual(selected, Harness.GROK)
        self.assertEqual(jobs[0]["harness"], "grok")
        self.assertEqual(dispatcher.calls, [Harness.DROID])
        self.assertEqual(repeated, (job_id, False, Harness.GROK))
        self.assertIn('"harness": "grok"', dispatcher.prompts[Harness.DROID])
        self.assertIn('"harness": "codex"', dispatcher.prompts[Harness.DROID])
        self.assertNotIn('"harness": "claude"', dispatcher.prompts[Harness.DROID])
        self.assertIn("Title: Build", dispatcher.prompts[Harness.DROID])
        self.assertEqual(len(dispatcher.closed_created_agents), 1)

    def test_auto_router_timeout_closes_controller_created_for_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(
                base,
                state_db=root / "state.db",
                planner=replace(
                    base.planner,
                    output_file=root / "plans/planner.json",
                ),
            )
            prompt_file = root / "task.md"
            prompt_file.write_text("Implement a focused change.", encoding="utf-8")
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "router",
                        AgentState.UNKNOWN,
                        False,
                        "w1:p2",
                        "prompt_acceptance_timeout",
                    )
                }
            )
            coordinator = Coordinator(
                config,
                dispatcher=dispatcher,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.GROK, Harness.CODEX),
            )

            with self.assertRaisesRegex(
                ValueError,
                "worker_selection_failed:prompt_acceptance_timeout",
            ):
                coordinator.enqueue_prompt_file(
                    harness=None,
                    title="Build",
                    prompt_file=prompt_file,
                    dedupe_key="auto-route-timeout",
                )

        self.assertEqual(len(dispatcher.closed_created_agents), 1)

    def test_explicit_enqueue_does_not_start_router(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
            )
            prompt_file = root / "task.md"
            prompt_file.write_text("Implement a focused change.", encoding="utf-8")
            dispatcher = FakeDispatcher({})
            coordinator = Coordinator(config, dispatcher=dispatcher)

            _, created, selected = coordinator.enqueue_prompt_file(
                harness=Harness.GROK,
                title="Build",
                prompt_file=prompt_file,
                dedupe_key="explicit-grok",
            )

        self.assertTrue(created)
        self.assertEqual(selected, Harness.GROK)
        self.assertEqual(dispatcher.calls, [])

    def test_ambiguous_task_uses_controller_topology_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(
                base,
                state_db=root / "state.db",
                planner=replace(
                    base.planner,
                    output_file=root / "plans/planner.json",
                ),
            )
            prompt_file = root / "task.md"
            prompt_file.write_text(
                "Determine the best execution approach.",
                encoding="utf-8",
            )
            dispatcher = FakeDispatcher(
                {
                    Harness.DROID: DispatchOutcome(
                        "controller",
                        AgentState.DONE,
                        True,
                        "w1:p1",
                    ),
                    Harness.GROK: DispatchOutcome(
                        "worker",
                        AgentState.DONE,
                        False,
                        "w1:p2",
                    ),
                },
                topology_placement="pane",
            )
            coordinator = Coordinator(
                config,
                dispatcher=dispatcher,
                controller_harness=Harness.DROID,
            )
            coordinator.enqueue_prompt_file(
                harness=Harness.GROK,
                title="Determine next step",
                prompt_file=prompt_file,
                dedupe_key="ambiguous-topology",
            )

            result = coordinator.run_once()
            job = coordinator.store.jobs(config.name)[0]

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(dispatcher.calls, [Harness.DROID, Harness.GROK])
        self.assertEqual(job["placement"], "pane")
        self.assertEqual(dispatcher.contexts[0].placement, PlacementTarget.TAB)
        self.assertEqual(dispatcher.contexts[1].placement, PlacementTarget.PANE)
        self.assertIsNotNone(dispatcher.contexts[1].batch_key)


def _job(workflow: str, harness: Harness, *, suffix: str = "") -> NewJob:
    key = f"{harness.value}-task{suffix}"
    return NewJob(
        workflow=workflow,
        title=key,
        harness=harness,
        prompt="Read only.",
        dedupe_key=key,
        max_attempts=2,
    )


if __name__ == "__main__":
    unittest.main()
