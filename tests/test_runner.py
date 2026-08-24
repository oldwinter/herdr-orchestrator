from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

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
    ) -> None:
        self.outcomes = outcomes
        self.route_output = route_output
        self.routed_harness = routed_harness
        self.topology_placement = topology_placement
        self.delay_seconds = delay_seconds
        self.calls: list[Harness] = []
        self.timeouts: list[float] = []
        self.prompts: dict[Harness, str] = {}
        self.contexts: list[DispatchContext | None] = []

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
        self.contexts.append(context)
        if self.route_output is not None and self.routed_harness is not None:
            self.route_output.write_text(
                json.dumps({"harness": self.routed_harness.value}),
                encoding="utf-8",
            )
        if (
            self.topology_placement is not None
            and "Choose the Herdr execution topology" in prompt
        ):
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
        return self.outcomes[harness]


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


class CoordinatorTests(unittest.TestCase):
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
                    Harness.DROID: DispatchOutcome(
                        "controller", AgentState.DONE, True, "w1:p1"
                    ),
                    Harness.GROK: DispatchOutcome(
                        "worker", AgentState.DONE, False, "w1:p2"
                    ),
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
                {
                    Harness.DROID: DispatchOutcome(
                        "controller", AgentState.DONE, True, "w1:p1"
                    )
                },
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
                    load_workflow(
                        REPO_ROOT / "workflows/multi-harness.toml"
                    ).planner,
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
