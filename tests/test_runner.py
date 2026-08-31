from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.herdr import replica_slot_names, worktree_agent_name
from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    AttemptRuntime,
    DispatchContext,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
)
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.store import Store, StoreError

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
        return self.outcomes[harness]

    def close_created_agent(self, name: str) -> None:
        self.closed_created_agents.append(name)


class PlannerDispatcher:
    def __init__(self, output_file: Path) -> None:
        self.output_file = output_file
        self.calls: list[Harness] = []
        self._lock = threading.Lock()

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        del prompt, timeout_seconds, agent_name, context
        with self._lock:
            self.calls.append(harness)
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.output_file.write_text('{"tasks": []}', encoding="utf-8")
        return DispatchOutcome("planner", AgentState.DONE, False, "w1:p1")


class ExplodingDispatcher(FakeDispatcher):
    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        del harness, prompt, timeout_seconds, agent_name, context
        raise RuntimeError("dispatcher exploded")


class RecoveryDispatcher(FakeDispatcher):
    def __init__(self, outcome: DispatchOutcome) -> None:
        super().__init__({})
        self.outcome = outcome
        self.recoveries: list[tuple[Harness, str, AttemptRuntime]] = []

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        del harness, prompt, timeout_seconds, agent_name, context
        raise AssertionError("accepted recovery must not dispatch another prompt")

    def recover(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
        context: DispatchContext,
        runtime: AttemptRuntime,
    ) -> DispatchOutcome:
        del timeout_seconds, agent_name, context
        self.recoveries.append((harness, prompt, runtime))
        return self.outcome


class LifecycleDispatcher(FakeDispatcher):
    def __init__(self, state_db: Path, outcome: DispatchOutcome) -> None:
        super().__init__({})
        self.state_db = state_db
        self.outcome = outcome
        self.persisted_phases: list[str] = []

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        del harness, prompt, timeout_seconds
        assert agent_name is not None and context is not None
        assert context.attempt_progress is not None
        for progress in (
            AttemptProgress(
                AttemptPhase.RUNTIME_ACQUIRED,
                agent_name,
                pane_id="w1:p2",
                herdr_workspace_id="w1",
                execution_path="/workspace",
                agent_session_id="session-1",
                prompt_baseline_sequence=10,
            ),
            AttemptProgress(
                AttemptPhase.PROMPT_ACCEPTED,
                agent_name,
                pane_id="w1:p2",
                herdr_workspace_id="w1",
                execution_path="/workspace",
                prompt_accepted_sequence=11,
                state_change_sequence=11,
                agent_state=AgentState.WORKING,
            ),
            AttemptProgress(
                AttemptPhase.SETTLED,
                agent_name,
                pane_id="w1:p2",
                agent_state=AgentState.DONE,
                state_change_sequence=12,
                agent_settled=True,
            ),
            AttemptProgress(
                AttemptPhase.RECEIPT_OBSERVED,
                agent_name,
                pane_id="w1:p2",
                agent_state=AgentState.DONE,
                state_change_sequence=12,
                agent_settled=True,
            ),
        ):
            context.attempt_progress(progress)
            with closing(sqlite3.connect(self.state_db)) as connection, connection:
                phase = connection.execute(
                    "SELECT phase FROM job_attempts ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            self.persisted_phases.append(phase)
        return self.outcome


class BaselineRecoveryDispatcher(RecoveryDispatcher):
    def recover(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
        context: DispatchContext,
        runtime: AttemptRuntime,
    ) -> DispatchOutcome:
        self.recoveries.append((harness, prompt, runtime))
        assert context.attempt_progress is not None
        context.attempt_progress(
            AttemptProgress(
                AttemptPhase.PROMPT_ACCEPTED,
                agent_name,
                pane_id=runtime.pane_id,
                herdr_workspace_id=runtime.herdr_workspace_id,
                execution_path=runtime.execution_path,
                agent_session_id=runtime.agent_session_id,
                prompt_baseline_sequence=runtime.prompt_baseline_sequence,
                prompt_accepted_sequence=11,
                state_change_sequence=11,
                agent_state=AgentState.WORKING,
            )
        )
        context.attempt_progress(
            AttemptProgress(
                AttemptPhase.SETTLED,
                agent_name,
                pane_id=runtime.pane_id,
                agent_state=AgentState.DONE,
                state_change_sequence=12,
                agent_settled=True,
            )
        )
        context.attempt_progress(
            AttemptProgress(
                AttemptPhase.RECEIPT_OBSERVED,
                agent_name,
                pane_id=runtime.pane_id,
                agent_state=AgentState.DONE,
                state_change_sequence=12,
                agent_settled=True,
            )
        )
        del timeout_seconds
        return self.outcome


class ResumeRecoveryDispatcher(RecoveryDispatcher):
    def __init__(self, outcome: DispatchOutcome) -> None:
        super().__init__(outcome)
        self.responses = 0

    def respond(self, *_: object, **__: object) -> DispatchOutcome:
        self.responses += 1
        raise AssertionError("accepted resume recovery must not send the response again")


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

    def test_dispatch_progress_is_committed_before_the_dispatcher_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            dispatcher = LifecycleDispatcher(
                config.state_db,
                DispatchOutcome(
                    "ignored-by-coordinator",
                    AgentState.DONE,
                    False,
                    "w1:p2",
                    placement=PlacementTarget.TAB,
                    execution_path="/workspace",
                    herdr_workspace_id="w1",
                    agent_settled=True,
                ),
            )
            observed: list[AttemptPhase] = []

            result = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                transition_observer=lambda transition: observed.append(transition.phase),
            ).run_once()

            with closing(sqlite3.connect(config.state_db)) as connection, connection:
                attempt = connection.execute("""
                    SELECT phase, prompt_baseline_sequence, prompt_accepted_sequence,
                           last_state_change_sequence, agent_session_id
                    FROM job_attempts
                    """).fetchone()

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(
            dispatcher.persisted_phases,
            [
                AttemptPhase.RUNTIME_ACQUIRED.value,
                AttemptPhase.PROMPT_ACCEPTED.value,
                AttemptPhase.SETTLED.value,
                AttemptPhase.RECEIPT_OBSERVED.value,
            ],
        )
        self.assertEqual(
            attempt,
            (AttemptPhase.OUTCOME_COMMITTED.value, 10, 11, 12, "session-1"),
        )

    def test_settled_unknown_receipt_failure_uses_retry_policy_not_attention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            dispatcher = LifecycleDispatcher(
                config.state_db,
                DispatchOutcome(
                    "settled-worker",
                    AgentState.UNKNOWN,
                    False,
                    "w1:p2",
                    "task_receipt_missing",
                    agent_settled=True,
                ),
            )
            observed: list[AttemptPhase] = []

            result = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                transition_observer=lambda transition: observed.append(transition.phase),
            ).run_once()

            job = store.jobs(config.name)[0]

        self.assertEqual(result["pending"], 1)
        self.assertEqual(job["state"], JobState.PENDING.value)
        self.assertEqual(job["attempt_phase"], AttemptPhase.ABANDONED.value)
        self.assertEqual(job["error_code"], "task_receipt_missing")
        self.assertEqual(observed[-1], AttemptPhase.ABANDONED)

    def test_unsettled_accepted_timeout_observer_reports_attention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            dispatcher = LifecycleDispatcher(
                config.state_db,
                DispatchOutcome(
                    "working-worker",
                    AgentState.UNKNOWN,
                    False,
                    "w1:p2",
                    "herdr_timeout",
                    agent_settled=False,
                ),
            )
            observed: list[AttemptPhase] = []

            result = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                transition_observer=lambda transition: observed.append(transition.phase),
            ).run_once()

        self.assertEqual(result["blocked"], 1)
        self.assertEqual(observed[-1], AttemptPhase.ATTENTION)

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

    def test_dispatcher_exception_preserves_claimed_agent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(_job(config.name, Harness.DROID))
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=ExplodingDispatcher({}),
            )

            result = coordinator.run_once()
            job = store.jobs(config.name)[0]
            with closing(sqlite3.connect(store.path)) as connection, connection:
                receipt = connection.execute(
                    "SELECT agent_name, error_code FROM receipts WHERE job_id = ?",
                    (job["id"],),
                ).fetchone()

        expected_name = replica_slot_names(
            config.name,
            config.workspace,
            Harness.DROID,
            1,
            PlacementTarget.TAB,
        )[0]
        self.assertEqual(result["pending"], 1)
        self.assertEqual(job["agent_name"], expected_name)
        self.assertEqual(receipt, (expected_name, "dispatcher_unhandled_error"))

    def test_run_once_adopts_an_accepted_expired_attempt_without_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            with patch("herdr_orchestrator.store.time.time", return_value=100.0):
                store.enqueue(_job(config.name, Harness.DROID))
                first = store.claim(
                    config.name,
                    limit=1,
                    lease_seconds=60,
                    slot_names={Harness.DROID.value: ("owned-droid",)},
                )[0]
                store.record_attempt_progress(
                    first,
                    AttemptProgress(
                        AttemptPhase.RUNTIME_ACQUIRED,
                        first.agent_name,
                        pane_id="w1:p2",
                        herdr_workspace_id="w1",
                        execution_path=str(config.workspace),
                        agent_session_id="session-1",
                        prompt_baseline_sequence=10,
                    ),
                )
                store.record_attempt_progress(
                    first,
                    AttemptProgress(
                        AttemptPhase.PROMPT_ACCEPTED,
                        first.agent_name,
                        pane_id="w1:p2",
                        herdr_workspace_id="w1",
                        execution_path=str(config.workspace),
                        agent_session_id="session-1",
                        prompt_accepted_sequence=11,
                        state_change_sequence=11,
                        agent_state=AgentState.WORKING,
                    ),
                )
            dispatcher = RecoveryDispatcher(
                DispatchOutcome(
                    "owned-droid",
                    AgentState.DONE,
                    True,
                    "w1:p2",
                    placement=PlacementTarget.TAB,
                    execution_path=str(config.workspace),
                    herdr_workspace_id="w1",
                    agent_settled=True,
                )
            )
            coordinator = Coordinator(config, store=store, dispatcher=dispatcher)

            with patch("herdr_orchestrator.store.time.time", return_value=161.0):
                result = coordinator.run_once()

            job = store.jobs(config.name)[0]

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(len(dispatcher.recoveries), 1)
        recovered_harness, recovered_prompt, runtime = dispatcher.recoveries[0]
        self.assertEqual(recovered_harness, Harness.DROID)
        self.assertIn("# Task packet\n\nRead only.", recovered_prompt)
        self.assertEqual(runtime.agent_name, "owned-droid")
        self.assertEqual(runtime.prompt_accepted_sequence, 11)

    def test_run_once_reconciles_runtime_baseline_before_assuming_unaccepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            with patch("herdr_orchestrator.store.time.time", return_value=100.0):
                store.enqueue(_job(config.name, Harness.DROID))
                first = store.claim(config.name, limit=1, lease_seconds=60)[0]
                store.record_attempt_progress(
                    first,
                    AttemptProgress(
                        AttemptPhase.RUNTIME_ACQUIRED,
                        first.agent_name,
                        pane_id="w1:p2",
                        herdr_workspace_id="w1",
                        execution_path=str(config.workspace),
                        agent_session_id="session-1",
                        prompt_baseline_sequence=10,
                        state_change_sequence=10,
                    ),
                )
            dispatcher = BaselineRecoveryDispatcher(
                DispatchOutcome(
                    first.agent_name,
                    AgentState.DONE,
                    True,
                    "w1:p2",
                    placement=PlacementTarget.TAB,
                    execution_path=str(config.workspace),
                    herdr_workspace_id="w1",
                    agent_settled=True,
                )
            )

            with patch("herdr_orchestrator.store.time.time", return_value=161.0):
                result = Coordinator(config, store=store, dispatcher=dispatcher).run_once()

            with closing(sqlite3.connect(config.state_db)) as connection, connection:
                attempt = connection.execute("""
                    SELECT attempt, phase, prompt_baseline_sequence,
                           prompt_accepted_sequence
                    FROM job_attempts
                    """).fetchone()

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(len(dispatcher.recoveries), 1)
        self.assertEqual(
            attempt,
            (1, AttemptPhase.OUTCOME_COMMITTED.value, 10, 11),
        )

    def test_planner_reservation_is_atomic_across_coordinators(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            planner_prompt_file = root / "planner.md"
            planner_prompt_file.write_text("Plan one task.", encoding="utf-8")
            config = replace(
                base,
                state_db=root / "state.db",
                planner=replace(
                    base.planner,
                    enabled=True,
                    interval_seconds=60,
                    prompt_file=planner_prompt_file,
                    output_file=root / "plans/planner.json",
                ),
            )
            stores = [Store(config.state_db), Store(config.state_db)]
            for store in stores:
                store.initialize()
            dispatcher = PlannerDispatcher(config.planner.output_file)
            coordinators = [
                Coordinator(
                    config,
                    store=store,
                    dispatcher=dispatcher,
                    controller_harness=Harness.DROID,
                )
                for store in stores
            ]
            barrier = threading.Barrier(2)
            metadata_float = Store.metadata_float

            def delayed_metadata_float(store: Store, key: str) -> float | None:
                value = metadata_float(store, key)
                with suppress(threading.BrokenBarrierError):
                    barrier.wait(timeout=2)
                return value

            with (
                patch.object(Store, "metadata_float", delayed_metadata_float),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [
                    executor.submit(coordinator._run_planner_if_due) for coordinator in coordinators
                ]
                for future in futures:
                    future.result()

        self.assertEqual(dispatcher.calls, [Harness.DROID])

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
            coordinator = Coordinator(config, store=store, dispatcher=dispatcher)
            job_id, _ = store.enqueue(_job(config.name, Harness.DROID))
            coordinator.run_once()

            result = coordinator.resume_blocked(job_id, "Approve this local action.")
            job = store.jobs(config.name)[0]

        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(dispatcher.calls, [Harness.DROID])
        self.assertEqual(len(dispatcher.responses), 1)
        response = dispatcher.responses[0]
        self.assertEqual(response[0], "droid-worker")
        self.assertEqual(response[2], "Approve this local action.")
        self.assertEqual(response[4], "w1:p2")

    def test_resume_blocked_recovers_an_accepted_operation_without_resending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            store = Store(config.state_db)
            store.initialize()
            with patch("herdr_orchestrator.store.time.time", return_value=50.0):
                store.enqueue(_job(config.name, Harness.DROID))
                claimed = store.claim(config.name, limit=1, lease_seconds=30)[0]
                store.record_outcome(
                    claimed,
                    DispatchOutcome(
                        claimed.agent_name,
                        AgentState.BLOCKED,
                        False,
                        "w1:p2",
                        "agent_blocked",
                        correlation_id=claimed.correlation_id,
                    ),
                )
            with patch("herdr_orchestrator.store.time.time", return_value=100.0):
                first_resume, _ = store.claim_blocked_for_resume(
                    config.name,
                    claimed.job_id,
                    lease_seconds=60,
                )
                store.record_attempt_progress(
                    first_resume,
                    AttemptProgress(
                        AttemptPhase.RUNTIME_ACQUIRED,
                        first_resume.agent_name,
                        pane_id="w1:p2",
                        herdr_workspace_id="w1",
                        execution_path=str(config.workspace),
                        prompt_baseline_sequence=20,
                    ),
                )
                store.record_attempt_progress(
                    first_resume,
                    AttemptProgress(
                        AttemptPhase.PROMPT_ACCEPTED,
                        first_resume.agent_name,
                        pane_id="w1:p2",
                        herdr_workspace_id="w1",
                        execution_path=str(config.workspace),
                        prompt_accepted_sequence=21,
                        state_change_sequence=21,
                    ),
                )
            dispatcher = ResumeRecoveryDispatcher(
                DispatchOutcome(
                    first_resume.agent_name,
                    AgentState.DONE,
                    True,
                    "w1:p2",
                    placement=PlacementTarget.TAB,
                    execution_path=str(config.workspace),
                    herdr_workspace_id="w1",
                    agent_settled=True,
                )
            )
            coordinator = Coordinator(config, store=store, dispatcher=dispatcher)

            with patch("herdr_orchestrator.store.time.time", return_value=161.0):
                result = coordinator.resume_blocked(
                    claimed.job_id,
                    "Approve this local action.",
                )

        self.assertEqual(result["state"], JobState.SUCCEEDED.value)
        self.assertEqual(dispatcher.responses, 0)
        self.assertEqual(len(dispatcher.recoveries), 1)
        assert dispatcher.recoveries[0][2].prompt_accepted_sequence == 21

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

    def test_gc_ignores_pane_ownership_claimed_only_by_a_stale_event(self) -> None:
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
                PlacementTarget.TAB,
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
                    "reused:pane",
                    placement=PlacementTarget.TAB,
                    correlation_id=claimed.correlation_id,
                ),
            )
            with self.assertRaisesRegex(StoreError, "job_lease_lost"):
                store.record_attempt_progress(
                    replace(claimed, lease_owner="stale-owner"),
                    AttemptProgress(
                        AttemptPhase.PROMPT_ACCEPTED,
                        name,
                        pane_id="not-owned:pane",
                        prompt_accepted_sequence=99,
                    ),
                )
            dispatcher = CleanupDispatcher()

            result = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
            ).gc_succeeded_agents(dry_run=False)

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["skipped_unowned"], 1)
        self.assertEqual(dispatcher.closed, [])

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
                title="Build",
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

    def test_enqueue_prompt_file_rejects_changed_dedupe_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
            )
            prompt_file = root / "task.md"
            prompt_file.write_text("Inspect the repository.", encoding="utf-8")
            coordinator = Coordinator(config, dispatcher=FakeDispatcher({}))
            coordinator.enqueue_prompt_file(
                harness=Harness.PI,
                title="Inspect",
                prompt_file=prompt_file,
                dedupe_key="contract-conflict",
            )

            with self.assertRaisesRegex(StoreError, "dedupe_contract_conflict"):
                coordinator.enqueue_prompt_file(
                    harness=Harness.PI,
                    title="Changed title",
                    prompt_file=prompt_file,
                    dedupe_key="contract-conflict",
                )

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
