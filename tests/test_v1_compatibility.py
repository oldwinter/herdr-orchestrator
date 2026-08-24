from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.cli import main
from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
)
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


class V1CompatibilityTests(unittest.TestCase):
    def test_v1_cli_shapes_remain_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _write_workflow(root, name="cli-contract", seed_jobs=True)
            empty_workflow = _write_workflow(root, name="run-contract", seed_jobs=False)

            doctor = _run_cli("doctor", workflow)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            doctor_payload = _json_stdout(doctor)
            self.assertIsInstance(doctor_payload["checks"], list)
            self.assertIsInstance(doctor_payload["ok"], bool)

            first_seed = _run_cli("seed", workflow)
            second_seed = _run_cli("seed", workflow)
            self.assertEqual(first_seed.returncode, 0, first_seed.stderr)
            self.assertEqual(second_seed.returncode, 0, second_seed.stderr)
            self.assertEqual(_json_stdout(first_seed), {"added": 1, "existing": 0})
            self.assertEqual(_json_stdout(second_seed), {"added": 0, "existing": 1})

            prompt_file = root / "enqueue.md"
            prompt_file.write_text("Read only.", encoding="utf-8")
            enqueue = _run_cli(
                "enqueue",
                workflow,
                "--harness",
                "droid",
                "--title",
                "CLI contract job",
                "--prompt-file",
                str(prompt_file),
                "--dedupe-key",
                "cli-contract-job",
            )
            self.assertEqual(enqueue.returncode, 0, enqueue.stderr)
            enqueue_payload = _json_stdout(enqueue)
            self.assertIsInstance(enqueue_payload["created"], bool)
            self.assertIsInstance(enqueue_payload["job_id"], str)
            self.assertTrue(enqueue_payload["created"])

            status = _run_cli("status", workflow)
            self.assertEqual(status.returncode, 0, status.stderr)
            status_payload = _json_stdout(status)
            self.assertEqual(status_payload["workflow"], "cli-contract")
            self.assertEqual(
                status_payload["counts"],
                {
                    "pending": 2,
                    "running": 0,
                    "succeeded": 0,
                    "blocked": 0,
                    "failed": 0,
                },
            )
            self.assertIsInstance(status_payload["jobs"], list)
            self.assertEqual(
                {
                    "id",
                    "title",
                    "harness",
                    "state",
                    "attempts",
                    "max_attempts",
                    "agent_name",
                    "error_code",
                },
                set(status_payload["jobs"][0]),
            )

            run_once = _run_cli("run", empty_workflow, "--once")
            self.assertEqual(run_once.returncode, 0, run_once.stderr)
            self.assertEqual(
                _json_stdout(run_once),
                {
                    "pending": 0,
                    "running": 0,
                    "succeeded": 0,
                    "blocked": 0,
                    "failed": 0,
                },
            )

            smoke_output = StringIO()
            with patch("herdr_orchestrator.cli.HerdrTransport", _SettledSmokeTransport):
                with redirect_stdout(smoke_output):
                    smoke_exit = main(
                        [
                            "smoke",
                            "--workflow",
                            str(workflow),
                            "--harness",
                            "droid",
                        ]
                    )
            self.assertEqual(smoke_exit, 0)
            smoke_payload = json.loads(smoke_output.getvalue())
            self.assertEqual(smoke_payload["failures"], [])
            self.assertEqual(smoke_payload["results"], [{"harness": "droid", "state": "done"}])

    def test_same_harness_replicas_preserve_slots_leases_and_single_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "state.db")
            store.initialize()
            for key in ("one", "two", "three"):
                store.enqueue(_job(key, Harness.GROK))

            baseline = time.time() + 1
            with patch("herdr_orchestrator.store.time.time", return_value=baseline):
                first_claim = store.claim(
                    "example",
                    limit=3,
                    lease_seconds=60,
                    slot_names={"grok": ("slot-a", "slot-b")},
                )
            self.assertEqual([job.agent_name for job in first_claim], ["slot-a", "slot-b"])
            self.assertEqual({job.attempt for job in first_claim}, {1})

            with patch("herdr_orchestrator.store.time.time", return_value=baseline + 30):
                self.assertEqual(
                    store.claim(
                        "example",
                        limit=3,
                        lease_seconds=60,
                        slot_names={"grok": ("slot-a", "slot-b")},
                    ),
                    [],
                )

            with patch("herdr_orchestrator.store.time.time", return_value=baseline + 30):
                settled_state = store.record_outcome(
                    first_claim[0],
                    DispatchOutcome("slot-a", AgentState.DONE, False, "pane:a"),
                )
                self.assertEqual(settled_state, JobState.SUCCEEDED)
                replacement = store.claim(
                    "example",
                    limit=3,
                    lease_seconds=60,
                    slot_names={"grok": ("slot-a", "slot-b")},
                )
            self.assertEqual(len(replacement), 1)
            self.assertEqual(replacement[0].agent_name, "slot-a")
            self.assertEqual(replacement[0].attempt, 1)

            with patch("herdr_orchestrator.store.time.time", return_value=baseline + 61):
                reclaimed = store.claim(
                    "example",
                    limit=3,
                    lease_seconds=60,
                    slot_names={"grok": ("slot-a", "slot-b")},
                )
            self.assertEqual(len(reclaimed), 1)
            self.assertEqual(reclaimed[0].agent_name, "slot-b")
            self.assertEqual(reclaimed[0].attempt, 2)

            with patch("herdr_orchestrator.store.time.time", return_value=baseline + 62):
                self.assertEqual(
                    store.record_outcome(
                        replacement[0],
                        DispatchOutcome("slot-a", AgentState.DONE, True, "pane:a"),
                    ),
                    JobState.SUCCEEDED,
                )
                self.assertEqual(
                    store.record_outcome(
                        reclaimed[0],
                        DispatchOutcome("slot-b", AgentState.DONE, True, "pane:b"),
                    ),
                    JobState.SUCCEEDED,
                )
            self.assertEqual(len(_receipts(store.path)), 3)

    def test_retry_blocked_and_settled_outcomes_have_durable_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "state.db")
            store.initialize()
            store.enqueue(_job("settled", Harness.DROID, max_attempts=1))
            store.enqueue(_job("blocked", Harness.PI, max_attempts=1))
            store.enqueue(_job("retry", Harness.CODEX, max_attempts=2))

            settled = store.claim("example", limit=3, lease_seconds=60)
            by_key = {job.dedupe_key: job for job in settled}

            self.assertEqual(
                store.record_outcome(
                    by_key["settled"],
                    DispatchOutcome("ho-droid", AgentState.DONE, False, "p:droid"),
                ),
                JobState.SUCCEEDED,
            )
            self.assertEqual(
                store.record_outcome(
                    by_key["blocked"],
                    DispatchOutcome(
                        "ho-pi",
                        AgentState.BLOCKED,
                        True,
                        "p:pi",
                        "agent_blocked",
                    ),
                ),
                JobState.BLOCKED,
            )
            self.assertEqual(
                store.record_outcome(
                    by_key["retry"],
                    DispatchOutcome(
                        "ho-codex",
                        AgentState.UNKNOWN,
                        False,
                        None,
                        "herdr_timeout",
                    ),
                ),
                JobState.PENDING,
            )

            retry_clock = time.time() + 3
            with patch("herdr_orchestrator.store.time.time", return_value=retry_clock):
                retry = store.claim("example", limit=1, lease_seconds=60)[0]
                self.assertEqual(retry.attempt, 2)
                self.assertEqual(
                    store.record_outcome(
                        retry,
                        DispatchOutcome(
                            "ho-codex",
                            AgentState.UNKNOWN,
                            True,
                            None,
                            "herdr_timeout",
                        ),
                    ),
                    JobState.FAILED,
                )

            receipts = _receipts(store.path)
            self.assertEqual(
                [(row["state"], row["agent_state"], row["error_code"]) for row in receipts],
                [
                    ("succeeded", "done", None),
                    ("blocked", "blocked", "agent_blocked"),
                    ("pending", "unknown", "herdr_timeout"),
                    ("failed", "unknown", "herdr_timeout"),
                ],
            )
            self.assertEqual(
                {int(row["job_id"]) for row in receipts},
                {by_key["settled"].job_id, by_key["blocked"].job_id, by_key["retry"].job_id},
            )

    def test_restart_preserves_job_ids_attempts_slots_status_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _write_workflow(
                root,
                name="restart-contract",
                seed_jobs=False,
                worker_harness="grok",
                replicas=2,
            )
            config = load_workflow(workflow)
            store_path = config.state_db
            store = Store(store_path)
            store.initialize()
            for key in ("one", "two", "three"):
                store.enqueue(_job(key, Harness.GROK, workflow=config.name))

            first_dispatcher = _RecordingDispatcher()
            first = Coordinator(config, store=store, dispatcher=first_dispatcher)
            first_result = first.run_once()
            first_jobs = store.jobs(config.name)

            second_dispatcher = _RecordingDispatcher()
            second_store = Store(store_path)
            second = Coordinator(config, store=second_store, dispatcher=second_dispatcher)
            second_result = second.run_once()
            second_jobs = second_store.jobs(config.name)

            self.assertEqual(first_result, {"pending": 0, "running": 0, "succeeded": 2, "blocked": 0, "failed": 0})
            self.assertEqual(second_result, {"pending": 0, "running": 0, "succeeded": 1, "blocked": 0, "failed": 0})
            self.assertEqual([job["id"] for job in first_jobs], [1, 2, 3])
            self.assertEqual([job["id"] for job in second_jobs], [1, 2, 3])
            self.assertEqual([job["attempts"] for job in second_jobs], [1, 1, 1])
            self.assertEqual(
                {call["agent_name"] for call in first_dispatcher.calls},
                set(_grok_slots(config)),
            )
            self.assertEqual(
                [call["agent_name"] for call in second_dispatcher.calls],
                [_grok_slots(config)[0]],
            )
            self.assertEqual(len(_receipts(store_path)), 3)


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
    ) -> DispatchOutcome:
        self.calls.append(
            {
                "harness": harness.value,
                "prompt": prompt,
                "timeout_seconds": timeout_seconds,
                "agent_name": agent_name,
            }
        )
        return DispatchOutcome(
            agent_name or "missing-agent",
            AgentState.DONE,
            False,
            f"pane:{agent_name}",
        )


class _SettledSmokeTransport:
    def __init__(self, workflow_name: str, workspace: Path) -> None:
        self.workflow_name = workflow_name
        self.workspace = workspace
        self.closed: list[str] = []

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
    ) -> DispatchOutcome:
        return DispatchOutcome(
            agent_name or f"smoke-{harness.value}",
            AgentState.DONE,
            False,
            f"pane:{harness.value}",
        )

    def close_created_agent(self, name: str) -> None:
        self.closed.append(name)


def _job(
    dedupe_key: str,
    harness: Harness,
    *,
    workflow: str = "example",
    max_attempts: int = 3,
) -> NewJob:
    return NewJob(
        workflow=workflow,
        title=dedupe_key,
        harness=harness,
        prompt="Read only.",
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )


def _write_workflow(
    root: Path,
    *,
    name: str,
    seed_jobs: bool,
    worker_harness: str = "droid",
    replicas: int = 1,
) -> Path:
    prompt = root / f"{name}.md"
    prompt.write_text("Read only.", encoding="utf-8")
    seed_block = (
        f"""
[[seed_jobs]]
title = "Seeded v1 contract job"
harness = "droid"
prompt_file = "{prompt.name}"
dedupe_key = "seeded-v1-contract"
"""
        if seed_jobs
        else ""
    )
    workflow = root / f"{name}.toml"
    workflow.write_text(
        f"""
schema_version = 1
name = "{name}"
workspace = "."
state_db = ".orchestrator/state.db"

[coordinator]
poll_seconds = 1
max_parallel = 2
lease_seconds = 120
max_attempts = 2
agent_timeout_seconds = 10

[planner]
enabled = false
harness = "droid"
interval_seconds = 60
prompt_file = "{prompt.name}"
output_file = ".orchestrator/plans/{name}.json"
max_tasks = 2

[[workers]]
name = "worker"
harness = "{worker_harness}"
capabilities = []
replicas = {replicas}
{seed_block}
""",
        encoding="utf-8",
    )
    return workflow


def _run_cli(command: str, workflow: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "compat:pane",
            "HERDR_WORKSPACE_ID": "compat-workspace",
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "herdr_orchestrator",
            command,
            "--workflow",
            str(workflow),
            *extra,
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _json_stdout(process: subprocess.CompletedProcess[str]) -> dict[str, object]:
    payload = json.loads(process.stdout)
    if not isinstance(payload, dict):
        raise AssertionError(f"expected JSON object, got {payload!r}")
    return payload


def _receipts(path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            """
            SELECT job_id, attempt, state, agent_state, error_code
            FROM receipts
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()


def _grok_slots(config: object) -> tuple[str, ...]:
    workers = getattr(config, "workers")
    worker = next(worker for worker in workers if worker.harness is Harness.GROK)
    from herdr_orchestrator.herdr import replica_slot_names

    return replica_slot_names(config.name, config.workspace, Harness.GROK, worker.replicas)


if __name__ == "__main__":
    unittest.main()
