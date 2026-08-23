from __future__ import annotations

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
    def __init__(self, outcomes: dict[Harness, DispatchOutcome]) -> None:
        self.outcomes = outcomes
        self.calls: list[Harness] = []

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
    ) -> DispatchOutcome:
        self.calls.append(harness)
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

    def test_seed_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            coordinator = Coordinator(config, dispatcher=FakeDispatcher({}))

            first = coordinator.seed()
            second = coordinator.seed()

        self.assertEqual(first, (5, 0))
        self.assertEqual(second, (0, 5))


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
