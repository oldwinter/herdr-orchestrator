from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.harness_health import (
    HarnessHealth,
    HarnessHealthError,
    HarnessHealthStatus,
    HealthSource,
)
from herdr_orchestrator.model import AgentState, DispatchOutcome, Harness, NewJob
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.selection import select_controller_harness
from herdr_orchestrator.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


class HarnessHealthTests(unittest.TestCase):
    def _config(self, root: Path):
        return replace(
            load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
            state_db=root / "state.db",
        )

    def test_unknown_refreshes_once_and_preference_uses_ready_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            store = Store(config.state_db)
            store.initialize()
            now = [100.0]
            calls: list[Harness] = []

            def probe(_config, harness, _timeout):
                calls.append(harness)
                return (
                    {"status": "ready"}
                    if harness is Harness.GROK
                    else {"status": "model_invalid", "error_code": "agent_model_invalid"}
                )

            health = HarnessHealth(
                store,
                config,
                clock=lambda: now[0],
                probe=probe,
                ttl_seconds=10,
                cooldown_seconds=5,
            )
            self.assertEqual(store.harness_health_rows(config.name, str(config.workspace)), [])
            snapshot = health.snapshot(
                (Harness.DROID, Harness.GROK),
                refresh=True,
            )

            self.assertEqual(snapshot.eligible_harnesses, (Harness.GROK,))
            self.assertEqual(
                {
                    str(row["harness"])
                    for row in store.harness_health_rows(
                        config.name,
                        str(config.workspace),
                    )
                },
                {"droid", "grok"},
            )
            self.assertEqual(calls, [Harness.DROID, Harness.GROK])
            self.assertEqual(
                snapshot.record_for(Harness.DROID).status,
                HarnessHealthStatus.UNAVAILABLE,
            )
            self.assertEqual(snapshot.record_for(Harness.GROK).source, HealthSource.PROBE.value)
            self.assertEqual(
                select_controller_harness(
                    config,
                    worker_harnesses=(Harness.DROID, Harness.GROK),
                    health=health,
                ),
                Harness.GROK,
            )

            now[0] = 109.0
            self.assertEqual(
                health.snapshot((Harness.GROK,), refresh=False).eligible_harnesses,
                (Harness.GROK,),
            )
            self.assertEqual(calls, [Harness.DROID, Harness.GROK])

    def test_expired_ready_evidence_refreshes_and_explicit_unhealthy_fails(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            now = [100.0]
            calls = 0

            def probe(_config, _harness, _timeout):
                nonlocal calls
                calls += 1
                return {"status": "ready"}

            health = HarnessHealth(
                Store(config.state_db),
                config,
                clock=lambda: now[0],
                probe=probe,
                ttl_seconds=5,
            )
            health.record_probe(Harness.DROID, {"status": "ready"})
            now[0] = 106.0
            self.assertEqual(
                health.snapshot((Harness.DROID,), refresh=True).record_for(Harness.DROID).status,
                HarnessHealthStatus.READY,
            )
            self.assertEqual(calls, 1)

            health.record_probe(
                Harness.CODEX,
                {"status": "unavailable", "error_code": "agent_auth_required"},
            )
            with self.assertRaisesRegex(HarnessHealthError, "worker_harness_unavailable:codex"):
                health.require(Harness.CODEX, role="worker")

    def test_dispatch_task_receipt_failure_refreshes_ready_but_blocked_is_neutral(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            health = HarnessHealth(Store(config.state_db), config)
            blocked = DispatchOutcome(
                "worker",
                AgentState.BLOCKED,
                False,
                "pane",
                error_code="agent_blocked",
            )
            self.assertIsNone(health.record_dispatch(Harness.DROID, blocked))
            self.assertEqual(
                health.snapshot((Harness.DROID,), refresh=False).record_for(Harness.DROID).status,
                HarnessHealthStatus.UNKNOWN,
            )
            receipt_failure = DispatchOutcome(
                "worker",
                AgentState.DONE,
                False,
                "pane",
                error_code="task_receipt_missing",
                agent_settled=True,
            )
            record = health.record_dispatch(Harness.DROID, receipt_failure)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, HarnessHealthStatus.READY)

    def test_probe_lease_deduplicates_concurrent_refreshes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            calls = 0
            lock = threading.Lock()
            entered = threading.Event()
            release = threading.Event()

            def probe(_config, _harness, _timeout):
                nonlocal calls
                with lock:
                    calls += 1
                entered.set()
                release.wait(timeout=2)
                return {"status": "ready"}

            first = HarnessHealth(Store(config.state_db), config, probe=probe)
            second = HarnessHealth(Store(config.state_db), config, probe=probe)
            with ThreadPoolExecutor(max_workers=2) as executor:
                future = executor.submit(first.snapshot, (Harness.GROK,), refresh=True)
                self.assertTrue(entered.wait(timeout=2))
                concurrent = second.snapshot((Harness.GROK,), refresh=True)
                release.set()
                completed = future.result(timeout=2)
            self.assertEqual(calls, 1)
            self.assertEqual(completed.eligible_harnesses, (Harness.GROK,))
            self.assertEqual(concurrent.eligible_harnesses, ())

    def test_coordinator_defers_unavailable_pending_job_without_attempt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow=config.name,
                    title="deferred",
                    harness=Harness.DROID,
                    prompt="task",
                    dedupe_key="health-deferred",
                    max_attempts=2,
                )
            )

            def probe(_config, _harness, _timeout):
                return {"status": "unavailable", "error_code": "agent_auth_required"}

            health = HarnessHealth(store, config, probe=probe)
            result = Coordinator(
                config,
                store=store,
                health=health,
                readiness_probe=probe,
            ).run_until_idle(timeout_seconds=2)
            job = store.jobs(config.name)[0]
            self.assertFalse(result["idle"])
            self.assertEqual(result["reason"], "degraded_capacity")
            self.assertEqual(result["claimed"], 0)
            self.assertEqual(job["state"], "pending")
            self.assertEqual(job["attempts"], 0)

    def test_ready_evidence_claims_same_pending_job_after_recovery(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow=config.name,
                    title="recovered",
                    harness=Harness.DROID,
                    prompt="task",
                    dedupe_key="health-recovered",
                    max_attempts=2,
                )
            )
            health = HarnessHealth(store, config)
            health.record_probe(
                Harness.DROID,
                {"status": "ready"},
                source=HealthSource.DOCTOR,
            )

            class Dispatcher:
                def dispatch(self, harness, prompt, **kwargs):
                    del prompt, kwargs
                    return DispatchOutcome(
                        f"{harness.value}-worker",
                        AgentState.DONE,
                        False,
                        "pane",
                        agent_settled=True,
                    )

            result = Coordinator(
                config,
                store=store,
                dispatcher=Dispatcher(),
                health=health,
            ).run_once()
            job = store.jobs(config.name)[0]
            self.assertEqual(result["claimed"], 1)
            self.assertEqual(job["attempts"], 1)
            self.assertEqual(job["state"], "succeeded")
            self.assertEqual(
                health.snapshot((Harness.DROID,), refresh=False).record_for(Harness.DROID).status,
                HarnessHealthStatus.READY,
            )


if __name__ == "__main__":
    unittest.main()
