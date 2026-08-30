from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.model import AgentState, DispatchOutcome, Harness
from herdr_orchestrator.readiness import HarnessHealthRegistry
from herdr_orchestrator.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


class HarnessHealthRegistryTests(unittest.TestCase):
    def test_concurrent_registries_share_one_durable_probe_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            started = threading.Event()
            release = threading.Event()
            calls = [0]

            def probe(
                workflow: object,
                harness: Harness,
                timeout: int,
            ) -> dict[str, object]:
                del workflow, harness, timeout
                calls[0] += 1
                started.set()
                release.wait(timeout=2)
                return {"status": "ready", "error_code": None, "error_summary": None}

            first = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                probe=probe,
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )
            second = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                probe=probe,
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
            )
            first_result: list[tuple[Harness, ...]] = []
            thread = threading.Thread(
                target=lambda: first_result.append(first.eligible((Harness.GROK,)))
            )
            thread.start()
            self.assertTrue(started.wait(timeout=2))

            concurrent = second.eligible((Harness.GROK,))
            release.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [1])
        self.assertEqual(concurrent, ())
        self.assertEqual(first_result, [(Harness.GROK,)])

    def test_fresh_evidence_survives_registry_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            first = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                clock=lambda: 100.0,
            )
            first.record_probe(
                Harness.GROK,
                {"status": "ready", "error_code": None, "error_summary": None},
            )

            restarted = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                clock=lambda: 200.0,
            )

            self.assertEqual(
                restarted.eligible((Harness.GROK,), refresh=False),
                (Harness.GROK,),
            )

    def test_refreshes_share_the_callers_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            monotonic = [0.0]
            timeouts: list[int] = []
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )

            def probe(
                workflow: object,
                harness: Harness,
                timeout: int,
            ) -> dict[str, object]:
                del workflow, harness
                timeouts.append(timeout)
                monotonic[0] += 8.0
                return {
                    "status": "timeout",
                    "error_code": "herdr_timeout",
                    "error_summary": None,
                }

            health = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                probe=probe,
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: 100.0,
                monotonic=lambda: monotonic[0],
            )

            eligible = health.eligible(
                (Harness.DROID, Harness.GROK),
                deadline=10.0,
            )

        self.assertEqual(eligible, ())
        self.assertEqual(timeouts, [10, 2])

    def test_dispatch_evidence_is_scoped_by_canonical_execution_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=root / "state.db",
            )
            health = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                clock=lambda: 100.0,
            )
            outcome = DispatchOutcome(
                "worker",
                AgentState.DONE,
                False,
                "w1:p1",
                agent_settled=True,
            )

            health.record_dispatch(
                Harness.GROK,
                outcome,
                workspace=root / "worktree-a",
            )
            matching = health.projection(
                (Harness.GROK,),
                workspace=root / "worktree-a/../worktree-a",
            )[0]
            distinct = health.projection(
                (Harness.GROK,),
                workspace=root / "worktree-b",
            )[0]

        self.assertEqual(matching["status"], "ready")
        self.assertTrue(matching["eligible"])
        self.assertEqual(distinct["status"], "unknown")
        self.assertFalse(distinct["eligible"])

    def test_expired_ready_evidence_projects_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = [100.0]
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            health = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                clock=lambda: clock[0],
            )
            health.record_probe(
                Harness.GROK,
                {"status": "ready", "error_code": None, "error_summary": None},
            )

            clock[0] = 1901.0
            projection = health.projection((Harness.GROK,))[0]

        self.assertEqual(projection["status"], "unknown")
        self.assertEqual(projection["reason_code"], "readiness_expired")
        self.assertFalse(projection["eligible"])

    def test_health_observations_emit_privacy_safe_metrics_and_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            observability = MagicMock()
            health = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                observability=observability,
                clock=lambda: 100.0,
            )
            result = {
                "status": "timeout",
                "error_code": "herdr_timeout",
                "error_summary": "private terminal output",
            }

            health.record_probe(Harness.GROK, result)
            health.record_probe(Harness.GROK, result)

        event_names = [call.args[0] for call in observability.event.call_args_list]
        self.assertEqual(event_names.count("harness_health_observed"), 2)
        self.assertEqual(event_names.count("harness_health_transition"), 1)
        transition_fields = observability.event.call_args_list[1].kwargs["fields"]
        self.assertEqual(transition_fields["harness"], "grok")
        self.assertEqual(transition_fields["status"], "degraded")
        self.assertEqual(transition_fields["reason_code"], "herdr_timeout")
        self.assertNotIn("error_summary", transition_fields)
        observability.metric.assert_called()

    def test_observability_failure_does_not_change_health_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            observability = MagicMock()
            observability.event.side_effect = RuntimeError("exporter failed")
            health = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                observability=observability,
                clock=lambda: 100.0,
            )

            health.record_probe(
                Harness.GROK,
                {"status": "ready", "error_code": None, "error_summary": None},
            )
            projection = health.projection((Harness.GROK,))[0]

        self.assertEqual(projection["status"], "ready")
        self.assertTrue(projection["eligible"])

    def test_dispatch_classification_separates_harness_and_task_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = [100.0]
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            health = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: clock[0],
            )

            health.record_probe(
                Harness.DROID,
                {"status": "ready", "error_code": None, "error_summary": None},
            )
            health.record_dispatch(
                Harness.DROID,
                _outcome(AgentState.BLOCKED, "agent_blocked"),
            )
            blocked = health.projection((Harness.DROID,))[0]
            health.record_dispatch(
                Harness.DROID,
                _outcome(AgentState.DONE, "task_receipt_missing"),
            )
            receipt_failure = health.projection((Harness.DROID,))[0]
            health.record_dispatch(
                Harness.DROID,
                _outcome(AgentState.UNKNOWN, "agent_provider_failed"),
            )
            provider_failure = health.projection((Harness.DROID,))[0]
            health.record_dispatch(
                Harness.DROID,
                _outcome(AgentState.UNKNOWN, "agent_auth_required"),
            )
            auth_failure = health.projection((Harness.DROID,))[0]
            health.record_dispatch(
                Harness.DROID,
                _outcome(AgentState.UNKNOWN, "agent_model_invalid"),
            )
            invalid_model = health.projection((Harness.DROID,))[0]
            health.record_dispatch(
                Harness.DROID,
                _outcome(AgentState.UNKNOWN, "prompt_acceptance_timeout"),
            )
            prompt_timeout = health.projection((Harness.DROID,))[0]
            health.record_dispatch(
                Harness.DROID,
                _outcome(AgentState.UNKNOWN, "agent_turn_not_observed"),
            )
            turn_not_observed = health.projection((Harness.DROID,))[0]
            health.record_dispatch(
                Harness.DROID,
                DispatchOutcome(
                    "agent",
                    AgentState.DONE,
                    False,
                    "w1:p1",
                    agent_settled=False,
                ),
            )
            unsettled = health.projection((Harness.DROID,))[0]

        self.assertEqual(blocked["status"], "ready")
        self.assertTrue(blocked["eligible"])
        self.assertEqual(receipt_failure["status"], "ready")
        self.assertTrue(receipt_failure["eligible"])
        self.assertEqual(provider_failure["status"], "degraded")
        self.assertEqual(provider_failure["reason_code"], "agent_provider_failed")
        self.assertFalse(provider_failure["eligible"])
        self.assertEqual(auth_failure["status"], "unavailable")
        self.assertEqual(auth_failure["reason_code"], "agent_auth_required")
        self.assertFalse(auth_failure["eligible"])
        self.assertNotIn("error_summary", auth_failure)
        self.assertEqual(invalid_model["status"], "unavailable")
        self.assertEqual(prompt_timeout["status"], "degraded")
        self.assertEqual(turn_not_observed["status"], "degraded")
        self.assertEqual(unsettled["status"], "degraded")

    def test_ready_evidence_expires_and_failure_cooldown_bounds_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = [100.0]
            probes = [
                {"status": "ready", "error_code": None, "error_summary": None},
                {
                    "status": "timeout",
                    "error_code": "herdr_timeout",
                    "error_summary": "private terminal output",
                },
                {"status": "ready", "error_code": None, "error_summary": None},
            ]
            config = replace(
                load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
                state_db=Path(temporary) / "state.db",
            )
            health = HarnessHealthRegistry(
                config,
                Store(config.state_db),
                probe=lambda workflow, harness, timeout: probes.pop(0),
                executable_finder=lambda command: f"/bin/{command}",
                clock=lambda: clock[0],
            )

            self.assertEqual(health.eligible((Harness.GROK,)), (Harness.GROK,))
            clock[0] = 1901.0
            self.assertEqual(health.eligible((Harness.GROK,)), ())
            degraded = health.projection((Harness.GROK,))[0]
            clock[0] = 2000.0
            self.assertEqual(health.eligible((Harness.GROK,)), ())
            clock[0] = 2202.0
            self.assertEqual(health.eligible((Harness.GROK,)), (Harness.GROK,))

        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(degraded["reason_code"], "herdr_timeout")
        self.assertEqual(probes, [])


def _outcome(state: AgentState, error_code: str) -> DispatchOutcome:
    return DispatchOutcome(
        agent_name="agent",
        state=state,
        member_reused=False,
        pane_id="w1:p1",
        error_code=error_code,
        error_summary="private terminal output",
    )


if __name__ == "__main__":
    unittest.main()
