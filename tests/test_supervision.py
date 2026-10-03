from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from herdr_orchestrator.cli import build_parser, main
from herdr_orchestrator.completion import CompletionPolicy
from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.drift import BaseDriftGuard
from herdr_orchestrator.model import AgentState, DispatchOutcome, Harness, NewJob
from herdr_orchestrator.orca import OrcaBridge, executable
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.store import SCHEMA_VERSION, Store, StoreError
from herdr_orchestrator.supervision import Supervision
from herdr_orchestrator.supervision_cli import _await_reply, worker_preamble

ROOT = Path(__file__).resolve().parents[1]


class SupervisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.db"
        self.store = Store(self.path)
        self.store.initialize()
        self.mail = Supervision(self.path, "test", "/workspace")

    def job(self, key: str, **changes: object) -> NewJob:
        return replace(
            NewJob("test", key, Harness.CODEX, "task", key, 2, workspace="/workspace"), **changes
        )

    def claim(self) -> object:
        return self.store.claim("test", limit=10, lease_seconds=60)

    def test_dependencies_are_atomic_scoped_immutable_and_cycle_free(self) -> None:
        parent, _ = self.store.enqueue(self.job("parent"))
        child, _ = self.store.enqueue(self.job("child", depends_on=(parent,)))
        self.assertEqual(
            self.store.enqueue(self.job("child", depends_on=(parent,))), (child, False)
        )
        with self.assertRaisesRegex(StoreError, "dedupe_contract_conflict"):
            self.store.enqueue(self.job("child"))
        for key, changes in (
            ("missing", {"depends_on": (999,)}),
            ("foreign", {"depends_on": (parent,), "workspace": "/other"}),
            ("invalid", {"depends_on": (True,)}),
            ("cycle", {"depends_on": (child + 1,)}),
        ):
            with self.subTest(key=key), self.assertRaises(StoreError):
                self.store.enqueue(self.job(key, **changes))
        self.assertEqual(len(self.store.jobs("test")), 2)
        self.assertEqual(self.store.jobs("test")[1]["depends_on"], [parent])

    def test_dependency_requires_verified_success_and_does_not_consume_attempt(self) -> None:
        parent, _ = self.store.enqueue(self.job("parent"))
        child, _ = self.store.enqueue(self.job("child", depends_on=(parent,)))
        first = self.store.claim("test", limit=10, lease_seconds=60)
        self.assertEqual([job.job_id for job in first], [parent])
        self.store.record_outcome(
            first[0], DispatchOutcome("ho-codex", AgentState.IDLE, False, "p")
        )
        self.assertEqual(self.claim(), [])
        self.assertEqual(self.store.jobs("test")[1]["attempts"], 0)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE jobs SET task_verified = 1 WHERE id = ?", (parent,))
        self.assertEqual([job.job_id for job in self.claim()], [child])

    def test_gates_survive_restart_and_resolve_idempotently(self) -> None:
        job, _ = self.store.enqueue(self.job("gated"))
        self.mail.create_gate(job, "approval", "Which approach?", ("A", "B"))
        self.mail.create_gate(job, "approval", "Which approach?", ("A", "B"))
        self.assertEqual(self.claim(), [])
        self.assertEqual(self.mail.constraints()[0]["waiting_for"], ["gate:approval"])
        restarted = Supervision(self.path, "test", "/workspace")
        with self.assertRaisesRegex(StoreError, "job_not_found"):
            Supervision(self.path, "test", "/other").resolve_gate("approval", "A")
        restarted.resolve_gate("approval", "A")
        restarted.resolve_gate("approval", "A")
        with self.assertRaisesRegex(StoreError, "gate_conflict"):
            restarted.resolve_gate("approval", "B")
        self.assertEqual(len(self.claim()), 1)
        with self.assertRaisesRegex(StoreError, "requires_unstarted"):
            self.mail.create_gate(job, "late", "Stop?")

    def test_gate_and_claim_race_never_claims_behind_pending_gate(self) -> None:
        job, _ = self.store.enqueue(self.job("race"))

        def gate() -> str:
            try:
                return self.mail.create_gate(job, "race-gate", "Approve?")
            except StoreError:
                return "already_claimed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            gate_future = pool.submit(gate)
            claim_future = pool.submit(self.claim)
        self.assertNotEqual(bool(claim_future.result()), gate_future.result() == "race-gate")

    def test_mail_replays_until_explicit_ack_and_ack_is_atomic(self) -> None:
        ids = self.mail.send(sender="one", recipient="two", body="hello", dedupe_key="k")
        self.assertEqual(
            ids, self.mail.send(sender="one", recipient="two", body="hello", dedupe_key="k")
        )
        first = self.mail.check("two")
        self.assertEqual(first, self.mail.check("two"))
        with self.assertRaisesRegex(StoreError, "ack_not_owned"):
            self.mail.acknowledge("two", (ids[0], 999))
        self.assertEqual(self.mail.check("two"), first)
        with self.assertRaisesRegex(StoreError, "ack_not_owned"):
            self.mail.acknowledge("other", tuple(ids))
        self.mail.acknowledge("two", tuple(ids))
        self.assertEqual(self.mail.check("two"), [])
        self.assertEqual(self.mail.acknowledge("two", tuple(ids)), 1)
        self.assertEqual(len(self.mail.check("two", include_acknowledged=True)), 1)
        with self.assertRaisesRegex(StoreError, "dedupe_conflict"):
            self.mail.send(sender="one", recipient="two", body="changed", dedupe_key="k")
        self.assertEqual(Supervision(self.path, "test", "/other").check("two"), [])

    def test_concurrent_send_is_deduplicated(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _: self.mail.send(
                        sender="one", recipient="two", body="same", dedupe_key="race"
                    ),
                    range(8),
                )
            )
        self.assertTrue(all(ids == results[0] for ids in results))
        self.assertEqual(len(self.mail.check("two")), 1)

    def test_broadcast_retry_freezes_membership(self) -> None:
        self.store.enqueue(self.job("codex"))
        self.store.claim("test", limit=1, lease_seconds=60)
        args = dict(sender="coordinator", recipient="@all", body="hello", dedupe_key="wave")
        ids = self.mail.send(**args)
        self.store.enqueue(self.job("pi", harness=Harness.PI))
        self.store.claim("test", limit=2, lease_seconds=60)
        self.assertEqual(self.mail.send(**args), ids)
        self.assertEqual(self.mail.check("ho-pi"), [])

    def test_runner_dag_releases_next_wave_only_after_verified_outcome(self) -> None:
        config = replace(
            load_workflow(ROOT / "workflows/multi-harness.toml"),
            name="test",
            state_db=self.path,
            workspace=Path("/workspace"),
        )
        from herdr_orchestrator.model import ReceiptKind, TaskReceipt

        parent, _ = self.store.enqueue(
            self.job("parent", receipt=TaskReceipt(ReceiptKind.FILE, "proof"))
        )
        child, _ = self.store.enqueue(self.job("child", depends_on=(parent,)))
        dispatcher = Mock(return_value=None)
        dispatcher.dispatch.return_value = DispatchOutcome(
            "ho-codex",
            AgentState.IDLE,
            False,
            "pane",
            agent_settled=True,
            task_verified=True,
        )
        coordinator = Coordinator(config, store=self.store, dispatcher=dispatcher)
        self.assertEqual(coordinator.run_once()["claimed"], 1)
        self.assertEqual(self.store.jobs("test")[1]["attempts"], 0)
        self.assertEqual(coordinator.run_once()["claimed"], 1)
        self.assertEqual([row["id"] for row in self.store.jobs("test")], [parent, child])
        self.assertEqual(self.store.jobs("test")[1]["state"], "succeeded")

    def test_dispatch_bound_messages_reject_missing_foreign_and_stale_identity(self) -> None:
        self.store.enqueue(self.job("active"))
        job = self.store.claim("test", limit=1, lease_seconds=60)[0]
        identity = {
            "job_id": job.job_id,
            "attempt_id": job.attempt_id,
            "fencing_token": job.fencing_token,
        }
        for changes in ({}, {**identity, "fencing_token": "old"}, {**identity, "attempt_id": 999}):
            with self.subTest(changes=changes), self.assertRaises(StoreError):
                self.mail.send(
                    sender=job.agent_name,
                    recipient="coordinator",
                    body="done",
                    dedupe_key="done",
                    kind="worker_done",
                    **changes,
                )
        self.mail.send(
            sender=job.agent_name,
            recipient="coordinator",
            body="done",
            dedupe_key="done",
            kind="worker_done",
            **identity,
        )
        self.assertEqual(self.store.jobs("test")[0]["state"], "running")
        self.assertIsNone(self.store.jobs("test")[0]["task_verified"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE job_attempts SET lease_until = 0")
        with self.assertRaisesRegex(StoreError, "stale_dispatch"):
            self.mail.send(
                sender=job.agent_name,
                recipient="coordinator",
                body="live",
                dedupe_key="heartbeat",
                kind="heartbeat",
                **identity,
            )

    def test_question_reply_ownership_correlation_and_timeout(self) -> None:
        self.store.enqueue(self.job("asking"))
        job = self.store.claim("test", limit=1, lease_seconds=60)[0]
        question = self.mail.send(
            sender=job.agent_name,
            recipient="coordinator",
            body="Which?",
            dedupe_key="question",
            kind="question",
            job_id=job.job_id,
            attempt_id=job.attempt_id,
            fencing_token=job.fencing_token,
        )[0]
        result, status = _await_reply(self.mail, job.agent_name, question, time.monotonic())
        self.assertEqual(status, 1)
        self.assertFalse(result["resend"])
        with self.assertRaisesRegex(StoreError, "reply_invalid"):
            self.mail.send(
                sender="foreign",
                recipient=job.agent_name,
                body="A",
                dedupe_key="reply",
                kind="reply",
                reply_to=question,
            )
        self.mail.send(
            sender="coordinator",
            recipient=job.agent_name,
            body="A",
            dedupe_key="reply",
            kind="reply",
            reply_to=question,
        )
        result, status = _await_reply(self.mail, job.agent_name, question, time.monotonic())
        self.assertEqual(status, 0)
        self.assertEqual(result["reply"]["body"], "A")
        self.assertEqual(len(self.mail.check(job.agent_name)), 1)

    def test_broadcast_excludes_sender_and_has_independent_ack(self) -> None:
        self.store.enqueue(self.job("codex"))
        self.store.enqueue(self.job("pi", harness=Harness.PI))
        jobs = self.store.claim("test", limit=2, lease_seconds=60)
        ids = self.mail.send(
            sender="coordinator", recipient="@all", body="status?", dedupe_key="all"
        )
        self.assertEqual(len(ids), 2)
        one, two = (self.mail.check(job.agent_name)[0] for job in jobs)
        self.assertEqual(one["thread_id"], two["thread_id"])
        self.mail.acknowledge(jobs[0].agent_name, (one["id"],))
        self.assertEqual(len(self.mail.check(jobs[1].agent_name)), 1)
        self.assertEqual(
            len(
                self.mail.send(
                    sender=jobs[0].agent_name,
                    recipient="@all",
                    body="hello",
                    dedupe_key="except-self",
                )
            ),
            1,
        )
        with self.assertRaisesRegex(StoreError, "group_invalid"):
            self.mail.send(sender="coordinator", recipient="@made-up", body="x", dedupe_key="bad")

    def test_input_bounds(self) -> None:
        for changes in (
            {"body": ""},
            {"body": "x" * 16001},
            {"sender": "../x"},
            {"kind": "shell"},
            {"priority": "highest"},
        ):
            kwargs = {"sender": "one", "recipient": "two", "body": "hello", "dedupe_key": "k"}
            with self.subTest(changes=changes), self.assertRaises(StoreError):
                self.mail.send(**(kwargs | changes))
        with self.assertRaises(StoreError):
            self.mail.check("two", limit=101)

    def test_v9_migration_preserves_jobs_receipts_and_dedupe(self) -> None:
        job = self.job("legacy")
        job_id, _ = self.store.enqueue(job)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            for table in ("job_dependencies", "supervision_messages", "supervision_gates"):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("UPDATE schema_meta SET version = 9")
        self.store.initialize()
        self.store.initialize()
        self.assertEqual(self.store.enqueue(job), (job_id, False))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT version FROM schema_meta").fetchone()[0], SCHEMA_VERSION
            )

    def test_cli_mail_roundtrip_and_gate_commands(self) -> None:
        config = replace(
            load_workflow(ROOT / "workflows/multi-harness.toml"),
            name="test",
            state_db=self.path,
            workspace=Path("/workspace"),
        )
        base = ["orchestration", "--workflow", str(config.path)]

        def run(args: list[str]) -> tuple[int, object]:
            output = io.StringIO()
            with (
                patch("herdr_orchestrator.cli.load_workflow", return_value=config),
                redirect_stdout(output),
            ):
                status = main(base + args)
            return status, json.loads(output.getvalue())

        status, result = run(
            ["send", "--from", "one", "--to", "two", "--body", "hello", "--key", "k"]
        )
        self.assertEqual(status, 0)
        self.assertEqual(run(["check", "--handle", "two"])[1]["messages"][0]["body"], "hello")
        self.assertEqual(
            run(["ack", "--handle", "two", "--id", str(result["message_ids"][0])])[0], 0
        )
        job_id, _ = self.store.enqueue(self.job("gate-cli"))
        self.assertEqual(
            run(["gate-create", "--job-id", str(job_id), "--id", "g", "--question", "OK?"])[0], 0
        )
        self.assertEqual(len(run(["gate-list"])[1]["gates"]), 1)
        self.assertEqual(run(["gate-resolve", "--id", "g", "--resolution", "yes"])[0], 0)
        self.assertEqual(run(["task-list"])[1]["constraints"], [])

    def test_runner_reports_constraint_wait_and_injects_decisions(self) -> None:
        config = replace(
            load_workflow(ROOT / "workflows/multi-harness.toml"),
            name="test",
            state_db=self.path,
            workspace=Path("/workspace"),
        )
        job_id, _ = self.store.enqueue(self.job("gated-run"))
        self.mail.create_gate(job_id, "review", "Which?", ("A", "B"))
        dispatcher = Mock()
        coordinator = Coordinator(config, store=self.store, dispatcher=dispatcher)
        result = coordinator.run_until_idle(timeout_seconds=10)
        self.assertEqual(result["reason"], "dependency_or_gate_wait")
        self.assertFalse(result["idle"])
        dispatcher.dispatch.assert_not_called()
        self.mail.resolve_gate("review", "A")
        job = self.store.claim("test", limit=1, lease_seconds=60)[0]
        prompt = worker_preamble(config, job)
        self.assertIn(job.fencing_token, prompt)
        self.assertIn('"resolution": "A"', prompt)
        self.assertIn("messages never replace", prompt)

    def test_enqueue_dependencies_reach_store_through_coordinator(self) -> None:
        config = replace(
            load_workflow(ROOT / "workflows/multi-harness.toml"),
            name="test",
            state_db=self.path,
            workspace=Path("/workspace"),
        )
        parent, _ = self.store.enqueue(self.job("parent"))
        prompt = Path(self.temporary.name) / "prompt.txt"
        prompt.write_text("task")
        coordinator = Coordinator(config, store=self.store, dispatcher=Mock())
        args = dict(
            harness=Harness.CODEX,
            title="child",
            prompt_file=prompt,
            dedupe_key="child",
            depends_on=(parent,),
            completion_policy=CompletionPolicy.STRUCTURED_V2,
        )
        child, created, _ = coordinator.enqueue_prompt_file(**args)
        self.assertTrue(created)
        self.assertEqual(coordinator.enqueue_prompt_file(**args)[0], child)
        self.assertEqual(self.store.jobs("test")[-1]["depends_on"], [parent])


class DriftTests(unittest.TestCase):
    def test_real_repository_drift_is_observed_without_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve()

            def git(*arguments: str) -> str:
                return subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.test",
                        *arguments,
                    ],
                    cwd=path,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            git("init", "-b", "main")
            git("commit", "--allow-empty", "-m", "base")
            git("branch", "worker")
            git("commit", "--allow-empty", "-m", "new base")
            git("switch", "worker")
            evidence = BaseDriftGuard("main", 0).observe(path)
            self.assertEqual(evidence["behind"], 1)
            self.assertFalse(evidence["eligible"])
            self.assertEqual(evidence["source"], "local_refs")
            self.assertTrue(BaseDriftGuard("main", 1).observe(path)["eligible"])
            self.assertEqual(
                BaseDriftGuard("missing", 0).observe(path)["reason"], "base_drift_unknown"
            )

    def test_guard_does_not_claim_or_spend_attempt_and_reports_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve()
            store = Store(path / "state.db")
            store.initialize()
            config = replace(
                load_workflow(ROOT / "workflows/multi-harness.toml"),
                name="drift",
                state_db=store.path,
                workspace=path,
            )
            store.enqueue(
                NewJob("drift", "task", Harness.CODEX, "task", "k", 2, workspace=str(path))
            )
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, "21\n", ""))
            dispatcher = Mock()
            coordinator = Coordinator(
                config,
                store=store,
                dispatcher=dispatcher,
                drift_guard=BaseDriftGuard("main", 20, runner),
            )
            report = coordinator.run_once()
            self.assertEqual(report["claimed"], 0)
            self.assertEqual(report["drift_deferred"][0]["reason"], "base_drift_exceeded")
            self.assertEqual(store.jobs("drift")[0]["attempts"], 0)
            self.assertEqual(
                coordinator.run_until_idle(timeout_seconds=10)["reason"], "base_drift_wait"
            )
            dispatcher.dispatch.assert_not_called()

    def test_guard_rejects_invalid_refs_and_handles_failures(self) -> None:
        for ref in ("--all", "HEAD..main", "main;touch", ""):
            with self.assertRaises(ValueError):
                BaseDriftGuard(ref, 0)
        with self.assertRaises(ValueError):
            BaseDriftGuard("main", -1)
        runner = Mock(side_effect=subprocess.TimeoutExpired("git", 5))
        self.assertFalse(BaseDriftGuard("main", 0, runner).observe(Path("/missing"))["eligible"])


class OrcaBridgeTests(unittest.TestCase):
    def test_read_only_native_receipt_and_fixed_argv(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"runs":[]}', ""))
        bridge = OrcaBridge(
            Path("/workspace"), runner=runner, environment={"ORCA_CLI_COMMAND": "/tool/orca"}
        )
        self.assertEqual(bridge.invoke(["run-list"]), (0, {"runs": []}))
        runner.assert_called_once_with(
            ["/tool/orca", "orchestration", "run-list", "--json"], cwd="/workspace", timeout=60
        )

    def test_write_guard_and_no_cleanup_escape(self) -> None:
        runner = Mock()
        bridge = OrcaBridge(Path("/workspace"), runner=runner, environment={})
        for arguments in (
            ["worker-start", "--spec", "x"],
            ["check"],
            ["check", "--peek", "--ack=x"],
            ["reset"],
            ["worker-stop"],
            ["worker-abandon"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                bridge.invoke(arguments)
        runner.assert_not_called()
        with self.assertRaisesRegex(ValueError, "authority_override"):
            bridge.invoke(["run-list", "--environment=other"])

    def test_native_failure_and_unknown_are_not_retried(self) -> None:
        receipt = {"failedStage": "submit", "residualResources": {"terminal": "t1"}}
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, json.dumps(receipt), "error"))
        bridge = OrcaBridge(Path("/workspace"), runner=runner, environment={})
        self.assertEqual(bridge.invoke(["worker-start", "--spec", "x"], apply=True), (1, receipt))
        runner.side_effect = subprocess.TimeoutExpired("orca", 10)
        status, payload = bridge.invoke(["worker-start", "--spec", "x"], apply=True)
        self.assertEqual(status, 2)
        self.assertEqual(payload["error"], "orca_outcome_unknown")
        self.assertEqual(runner.call_count, 2)

    def test_malformed_unavailable_and_timeout_bounds(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '"not an object"', ""))
        bridge = OrcaBridge(Path("/workspace"), runner=runner, environment={})
        self.assertEqual(bridge.invoke(["run-list"])[0], 2)
        runner.side_effect = OSError()
        self.assertEqual(bridge.invoke(["run-list"])[1]["error"], "orca_unavailable")
        with self.assertRaises(ValueError):
            bridge.invoke(["run-list"], timeout_seconds=0)

    def test_linux_does_not_launch_screen_reader(self) -> None:
        with patch("herdr_orchestrator.orca.sys.platform", "linux"):
            self.assertEqual(executable({}), "orca-ide")
            self.assertEqual(executable({"ORCA_DEV_REPO_ROOT": "/repo"}), "orca-dev")

    def test_parser_keeps_native_flags_and_spec_as_data(self) -> None:
        args = build_parser().parse_args(
            [
                "orca",
                "--workflow",
                "w.toml",
                "--apply",
                "worker-start",
                "--spec",
                "x; touch /tmp/never",
                "--agent",
                "codex",
            ]
        )
        self.assertTrue(args.apply)
        self.assertEqual(
            args.arguments, ["worker-start", "--spec", "x; touch /tmp/never", "--agent", "codex"]
        )

    def test_error_receipt_from_stderr_is_preserved(self) -> None:
        receipt = {"ok": False, "error": {"code": "runtime_unavailable"}}
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", json.dumps(receipt)))
        bridge = OrcaBridge(Path("/workspace"), runner=runner, environment={})
        self.assertEqual(bridge.invoke(["run-list"]), (1, receipt))
