from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    NewJob,
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
    ) -> None:
        self.outcomes = outcomes
        self.route_output = route_output
        self.routed_harness = routed_harness
        self.calls: list[Harness] = []
        self.prompts: dict[Harness, str] = {}

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
    ) -> DispatchOutcome:
        self.calls.append(harness)
        self.prompts[harness] = prompt
        if self.route_output is not None and self.routed_harness is not None:
            self.route_output.write_text(
                json.dumps({"harness": self.routed_harness.value}),
                encoding="utf-8",
            )
        return self.outcomes[harness]


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
        self.assertCountEqual(dispatcher.calls, [Harness.DROID, Harness.CLAUDE])
        self.assertIn("# Factory Droid execution profile", dispatcher.prompts[Harness.DROID])
        self.assertIn("# Claude Code execution profile", dispatcher.prompts[Harness.CLAUDE])
        self.assertIn("# Task packet\n\nRead only.", dispatcher.prompts[Harness.DROID])
        self.assertNotIn("Hermes Agent execution profile", dispatcher.prompts[Harness.DROID])

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


def _job(workflow: str, harness: Harness) -> NewJob:
    return NewJob(
        workflow=workflow,
        title=f"{harness.value} task",
        harness=harness,
        prompt="Read only.",
        dedupe_key=f"{harness.value}-task",
        max_attempts=2,
    )


if __name__ == "__main__":
    unittest.main()
