from __future__ import annotations

import io
import json
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from herdr_orchestrator.cli import doctor
from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.delivery import DeliveryError, StandardizedDelivery
from herdr_orchestrator.harness_health import (
    HarnessHealth,
    HarnessHealthError,
    HarnessHealthStatus,
    HealthSource,
)
from herdr_orchestrator.herdr import doctor_agent_name, smoke_agent_name
from herdr_orchestrator.model import AgentState, DispatchOutcome, Harness, NewJob
from herdr_orchestrator.observability import Observability
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
                allow_live_probe=True,
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
                allow_live_probe=True,
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

    def test_blocked_hard_failure_updates_health_but_task_block_is_neutral(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            health = HarnessHealth(Store(config.state_db), config)
            health.record_probe(Harness.DROID, {"status": "ready"})

            task_block = DispatchOutcome(
                "worker",
                AgentState.BLOCKED,
                False,
                "pane",
                error_code="agent_blocked",
            )
            self.assertIsNone(health.record_dispatch(Harness.DROID, task_block))
            self.assertEqual(
                health.snapshot((Harness.DROID,), refresh=False).record_for(Harness.DROID).status,
                HarnessHealthStatus.READY,
            )

            auth_block = replace(task_block, error_code="agent_auth_required")
            record = health.record_dispatch(Harness.DROID, auth_block)
            assert record is not None
            self.assertEqual(record.status, HarnessHealthStatus.UNAVAILABLE)
            self.assertEqual(record.reason, "agent_auth_required")

    def test_non_cooperative_probe_is_bounded_and_late_ready_does_not_authorize(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            started = threading.Event()
            release = threading.Event()

            def probe(_workflow, _harness, _timeout):
                started.set()
                release.wait(timeout=30)
                return {"status": "ready"}

            health = HarnessHealth(
                Store(config.state_db),
                config,
                probe=probe,
                allow_live_probe=True,
                probe_timeout_seconds=5,
            )
            deadline = time.monotonic() + 5.1
            began = time.monotonic()
            snapshot = health.snapshot(
                (Harness.DROID,),
                refresh=True,
                timeout_seconds=5,
                deadline=deadline,
            )
            elapsed = time.monotonic() - began

            self.assertTrue(started.is_set())
            self.assertLess(elapsed, 6.0)
            self.assertEqual(snapshot.eligible_harnesses, ())
            persisted = health.snapshot((Harness.DROID,), refresh=False).record_for(Harness.DROID)
            self.assertEqual(persisted.status, HarnessHealthStatus.DEGRADED)
            self.assertEqual(persisted.reason, "timeout")
            release.set()

    def test_live_probe_deadline_terminates_isolated_probe_process(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))

            def probe(_workflow, _harness, _timeout):
                time.sleep(30)
                return {"status": "ready"}

            health = HarnessHealth(
                Store(config.state_db),
                config,
                probe=probe,
                allow_live_probe=True,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_PANE_ID": "w:p",
                    "HERDR_WORKSPACE_ID": "w",
                },
                executable_finder=lambda command: f"/bin/{command}",
                isolate_probes=True,
                probe_timeout_seconds=5,
            )
            began = time.monotonic()
            snapshot = health.snapshot(
                (Harness.DROID,),
                refresh=True,
                timeout_seconds=5,
                deadline=time.monotonic() + 5.1,
            )

            self.assertLess(time.monotonic() - began, 6.0)
            self.assertEqual(snapshot.eligible_harnesses, ())
            self.assertEqual(
                health.snapshot((Harness.DROID,), refresh=False).record_for(Harness.DROID).reason,
                "timeout",
            )

    def test_injected_ci_disables_and_persists_probe_classification(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            calls = 0

            def probe(_workflow, _harness, _timeout):
                nonlocal calls
                calls += 1
                return {"status": "ready"}

            health = HarnessHealth(
                Store(config.state_db),
                config,
                probe=probe,
                environment={"CI": "1"},
            )
            result = health.run_probe(Harness.DROID, probe)

            self.assertEqual(calls, 0)
            self.assertEqual(result["error_code"], "readiness_ci_forbidden")
            record = health.snapshot((Harness.DROID,), refresh=False).record_for(Harness.DROID)
            self.assertEqual(record.status, HarnessHealthStatus.UNAVAILABLE)
            self.assertEqual(record.reason, "readiness_ci_forbidden")

    def test_health_report_exposes_static_exclusion_over_persisted_ready(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            health = HarnessHealth(
                Store(config.state_db),
                config,
                environment={"HERDR_ENV": "0"},
            )
            health.record_probe(Harness.DROID, {"status": "ready"})

            report = health.snapshot((Harness.DROID,), refresh=False).public_json()

            self.assertEqual(report["eligible"], [])
            self.assertEqual(report["static_reasons"], {"droid": "not_in_herdr"})
            self.assertEqual(report["records"][0]["source"], "preflight")

    def test_malformed_probe_result_is_stable_for_doctor_checks(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            output = io.StringIO()

            def probe(_workflow, _harness, _timeout):
                return "malformed"

            with redirect_stdout(output):
                result = doctor(
                    config,
                    environ={
                        "HERDR_ENV": "1",
                        "HERDR_PANE_ID": "w:p",
                        "HERDR_WORKSPACE_ID": "w",
                    },
                    which=lambda command: f"/bin/{command}",
                    version_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                        ["herdr", "--version"], 0, "herdr 0.8.2\n", ""
                    ),
                    readiness_probe=probe,
                    selected_harnesses=["droid"],
                    store=Store(config.state_db),
                )

            report = json.loads(output.getvalue())
            readiness = next(
                check for check in report["checks"] if check["check"] == "readiness:droid"
            )
            self.assertEqual(result, 1)
            self.assertFalse(readiness["ok"])
            self.assertEqual(readiness["error_code"], "readiness_result_invalid")

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

            first = HarnessHealth(
                Store(config.state_db), config, probe=probe, allow_live_probe=True
            )
            second = HarnessHealth(
                Store(config.state_db), config, probe=probe, allow_live_probe=True
            )
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
                    workspace=str(config.workspace.resolve()),
                    title="deferred",
                    harness=Harness.DROID,
                    prompt="task",
                    dedupe_key="health-deferred",
                    max_attempts=2,
                )
            )

            def probe(_config, _harness, _timeout):
                return {"status": "unavailable", "error_code": "agent_auth_required"}

            health = HarnessHealth(store, config, probe=probe, allow_live_probe=True)
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

    def test_run_report_reads_post_dispatch_health_without_changing_routing_wave(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow=config.name,
                    workspace=str(config.workspace.resolve()),
                    title="post-dispatch-health",
                    harness=Harness.DROID,
                    prompt="task",
                    dedupe_key="post-dispatch-health",
                    max_attempts=1,
                )
            )
            health = HarnessHealth(store, config)
            health.record_probe(Harness.DROID, {"status": "ready"})

            class Dispatcher:
                def dispatch(self, *_args, **_kwargs):
                    return DispatchOutcome(
                        "droid-worker",
                        AgentState.UNKNOWN,
                        False,
                        None,
                        error_code="agent_auth_required",
                    )

            report = Coordinator(
                config,
                store=store,
                dispatcher=Dispatcher(),
                health=health,
            ).run_once()

            current = report["harness_health"]["records"][0]
            routed = report["routing_snapshot"]["records"][0]
            self.assertEqual(current["status"], HarnessHealthStatus.UNAVAILABLE.value)
            self.assertEqual(routed["status"], HarnessHealthStatus.READY.value)

    def test_static_preflight_is_effective_only_and_does_not_poison_ready_evidence(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            store = Store(config.state_db)
            health = HarnessHealth(
                store,
                config,
                environment={"HERDR_ENV": "0"},
                probe=lambda *_: (_ for _ in ()).throw(AssertionError("probe not expected")),
            )
            health.record_probe(Harness.DROID, {"status": "ready"})

            with self.assertRaisesRegex(ValueError, "controller_harness_unavailable:droid"):
                select_controller_harness(
                    config,
                    worker_harnesses=(Harness.DROID,),
                    health=health,
                )
            persisted = store.harness_health_rows(config.name, str(config.workspace))[0]
            self.assertEqual(persisted["status"], "ready")

    def test_stale_dispatch_evidence_cannot_replace_newer_health(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            health = HarnessHealth(Store(config.state_db), config, clock=lambda: 100.0)
            health.record_probe(Harness.DROID, {"status": "ready"}, observed_at=100.0)
            health.record_probe(
                Harness.DROID,
                {"status": "unavailable", "error_code": "agent_auth_required"},
                observed_at=99.0,
            )
            current = health.snapshot((Harness.DROID,), refresh=False).record_for(Harness.DROID)
            self.assertEqual(current.status, HarnessHealthStatus.READY)

    def test_unknown_settled_error_is_degraded_not_ready(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            health = HarnessHealth(Store(config.state_db), config)
            outcome = DispatchOutcome(
                "worker",
                AgentState.DONE,
                False,
                "pane",
                error_code="new_runtime_error",
                agent_settled=True,
            )
            record = health.record_dispatch(Harness.DROID, outcome)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, HarnessHealthStatus.DEGRADED)

    def test_pending_harness_projection_respects_workspace_affinity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            store = Store(config.state_db)
            store.initialize()
            for workspace, key in (
                ("/workspace/a", "workspace-a"),
                ("/workspace/b", "workspace-b"),
            ):
                store.enqueue(
                    NewJob(
                        workflow=config.name,
                        workspace=workspace,
                        title=key,
                        harness=Harness.DROID,
                        prompt="task",
                        dedupe_key=key,
                        max_attempts=2,
                    )
                )
            self.assertEqual(
                store.pending_harnesses(config.name, workspace="/workspace/a"),
                (Harness.DROID,),
            )
            with store._connect() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE workspace = ?",
                    ("/workspace/a",),
                ).fetchone()
            self.assertEqual(row[0], 1)

    def test_doctor_forces_fresh_health_probe_through_the_shared_store(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            store = Store(config.state_db)
            calls: list[Harness] = []

            def probe(_workflow, harness, _timeout):
                calls.append(harness)
                return {"status": "ready"}

            output = io.StringIO()
            with redirect_stdout(output):
                result = doctor(
                    config,
                    environ={
                        "HERDR_ENV": "1",
                        "HERDR_PANE_ID": "w:p",
                        "HERDR_WORKSPACE_ID": "w",
                    },
                    which=lambda name: f"/bin/{name}",
                    version_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                        ["herdr", "--version"],
                        0,
                        "herdr 0.8.2\n",
                        "",
                    ),
                    readiness_probe=probe,
                    selected_harnesses=["droid"],
                    store=store,
                )
            self.assertEqual(result, 0)
            self.assertEqual(calls, [Harness.DROID])
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["harness_health"]["records"][0]["source"], "doctor")

    def test_delivery_worker_dispatch_rechecks_health_before_transport(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            store = Store(config.state_db)
            health = HarnessHealth(store, config)
            health.record_probe(Harness.DROID, {"status": "ready"})
            health.record_probe(
                Harness.CODEX,
                {"status": "unavailable", "error_code": "agent_auth_required"},
            )

            class Dispatcher:
                def dispatch(self, *args, **kwargs):
                    raise AssertionError("unhealthy worker must not reach transport")

            delivery = StandardizedDelivery(
                config,
                dispatcher=Dispatcher(),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID, Harness.CODEX),
                health=health,
            )
            delivery._run_root = root / "delivery"
            delivery._run_root.mkdir()
            with self.assertRaisesRegex(DeliveryError, "worker_harness_unavailable:codex"):
                delivery._run_artifact_dispatch(
                    config.workspace,
                    Harness.CODEX,
                    "task",
                    delivery._run_root / "artifact.json",
                    role="worker",
                    use_principal_proxy=False,
                    agent_name_override=None,
                )

    def test_drain_refreshes_share_the_global_deadline(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            store = Store(config.state_db)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow=config.name,
                    title="deadline",
                    harness=Harness.DROID,
                    prompt="task",
                    dedupe_key="health-deadline",
                    max_attempts=2,
                )
            )
            calls: list[Harness] = []

            def probe(_workflow, harness, _timeout):
                calls.append(harness)
                time.sleep(1.2)
                return {"status": "ready"}

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

            health = HarnessHealth(store, config, probe=probe, allow_live_probe=True)
            started = time.monotonic()
            result = Coordinator(
                config,
                store=store,
                dispatcher=Dispatcher(),
                health=health,
                readiness_probe=probe,
            ).run_until_idle(timeout_seconds=6)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 3)
            self.assertEqual(calls, [Harness.DROID])
            self.assertTrue(result["idle"])

    def test_health_telemetry_has_no_prompt_or_terminal_material(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            telemetry = root / "telemetry"
            health = HarnessHealth(
                Store(config.state_db),
                config,
                observability=Observability(telemetry, config.name, clock=lambda: 100.0),
                clock=lambda: 100.0,
            )
            health.record_probe(
                Harness.DROID,
                {
                    "status": "ready",
                    "prompt": "secret prompt must not persist",
                    "terminal_output": "secret transcript",
                },
            )
            health.snapshot((Harness.DROID,), refresh=False)
            events = (telemetry / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("secret prompt", events)
            self.assertNotIn("secret transcript", events)
            self.assertIn("harness_health_changed", events)

    def test_probe_names_include_workspace_identity(self) -> None:
        workspace_a = Path("/workspace/a")
        workspace_b = Path("/workspace/b")
        self.assertNotEqual(
            doctor_agent_name("example", Harness.DROID, workspace_a),
            doctor_agent_name("example", Harness.DROID, workspace_b),
        )
        self.assertNotEqual(
            smoke_agent_name("example", Harness.DROID, workspace_a),
            smoke_agent_name("example", Harness.DROID, workspace_b),
        )


if __name__ == "__main__":
    unittest.main()
