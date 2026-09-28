from __future__ import annotations

import concurrent.futures
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

from herdr_orchestrator.completion import ReceiptKind, TaskReceipt
from herdr_orchestrator.model import (
    Harness,
    JobState,
    NewJob,
    PlacementTarget,
)
from herdr_orchestrator.store import Store, StoreError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/devin_factory.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("devin_factory_regression", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


devin_factory = _load_script()


class BacklogFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / ".orchestrator" / "plans").mkdir(parents=True)
        (self.root / "factory" / "prompts").mkdir(parents=True)
        (self.root / "factory" / "prompts" / "planner.md").write_text(
            "planner base", encoding="utf-8"
        )
        self.workflow = self.root / "workflow.toml"
        self.backlog = self.root / "factory" / "backlog.toml"
        self.store = Store(self.root / "state.db")
        self.store.initialize()

    def write_workflow(self, max_attempts: int = 2) -> None:
        profiles = (REPO_ROOT / "profiles" / "harnesses").as_posix()
        self.workflow.write_text(
            "\n".join(
                [
                    "schema_version = 1",
                    'name = "factory-test"',
                    'workspace = "."',
                    'state_db = "state.db"',
                    f'profiles_dir = "{profiles}"',
                    "[coordinator]",
                    "poll_seconds = 1",
                    "max_parallel = 1",
                    "lease_seconds = 120",
                    f"max_attempts = {max_attempts}",
                    "agent_timeout_seconds = 30",
                    "[placement]",
                    'mode = "pane"',
                    "[planner]",
                    "enabled = false",
                    'harness = "auto"',
                    'worker_harnesses = ["codex"]',
                    "interval_seconds = 3600",
                    'prompt_file = "factory/prompts/planner.md"',
                    'output_file = ".orchestrator/plans/factory.json"',
                    "max_tasks = 10",
                    "[[workers]]",
                    'name = "implementation"',
                    'harness = "codex"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def write_prompt(self, name: str, text: str = "task contract") -> None:
        (self.root / "factory" / "prompts" / f"{name}.md").write_text(text, encoding="utf-8")

    def write_backlog(self, body: str) -> None:
        self.backlog.write_text("schema_version = 1\n\n" + body, encoding="utf-8")

    def item_header(
        self,
        dedupe_key: str,
        *,
        receipt: str | None = None,
        requires: list[str] | None = None,
        max_attempts: int | None = None,
    ) -> str:
        receipt_line = (
            f'receipt = "{receipt}"\n'
            if receipt is not None
            else f'receipt = ".orchestrator/factory/receipts/{dedupe_key}.json"\n'
        )
        requires_line = f"requires = {json.dumps(requires)}\n" if requires is not None else ""
        attempts_line = f"max_attempts = {max_attempts}\n" if max_attempts is not None else ""
        return (
            "[[items]]\n"
            f'dedupe_key = "{dedupe_key}"\n'
            f'title = "item {dedupe_key}"\n'
            'harness = "codex"\n'
            f'prompt_file = "prompts/{dedupe_key}.md"\n'
            f"{receipt_line}"
            f"{requires_line}"
            f"{attempts_line}"
        )

    def item_toml(
        self,
        dedupe_key: str,
        argv: str,
        *,
        timeout: int = 60,
        receipt: str | None = None,
        requires: list[str] | None = None,
        max_attempts: int | None = None,
    ) -> str:
        return (
            self.item_header(
                dedupe_key,
                receipt=receipt,
                requires=requires,
                max_attempts=max_attempts,
            )
            + "[[items.checks]]\n"
            + f"argv = {argv}\n"
            + f"timeout_seconds = {timeout}\n"
        )

    def jobs(self, workflow: str = "factory-test") -> list[dict[str, object]]:
        return self.store.jobs(workflow, workspace=str(self.root))

    def load_items(self) -> dict[str, object]:
        return devin_factory.load_backlog(self.backlog)


class BacklogLoaderTests(BacklogFixture):
    def test_loads_items_with_explicit_checks(self) -> None:
        self.write_prompt("alpha")
        self.write_backlog(self.item_toml("alpha", '["python3", "-c", "pass"]'))
        items = devin_factory.load_backlog(self.backlog)
        item = items["alpha"]
        self.assertEqual(item.checks[0].argv, ("python3", "-c", "pass"))
        self.assertEqual(item.receipt, ".orchestrator/factory/receipts/alpha.json")

    def test_rejects_invalid_items(self) -> None:
        self.write_prompt("alpha")
        valid_check = '[[items.checks]]\nargv = ["python3", "-c", "pass"]\n'
        cases = {
            "unknown key": self.item_header("alpha") + "typo_key = true\n" + valid_check,
            "empty checks": self.item_header("alpha") + "checks = []\n",
            "bad dedupe": self.item_header("bad key!") + valid_check,
            "absolute receipt": self.item_header("alpha", receipt="/tmp/x.json") + valid_check,
            "escape receipt": self.item_header("alpha", receipt="../x.json") + valid_check,
            "receipt outside runtime": self.item_header("alpha", receipt="docs/x.json")
            + valid_check,
            "unknown check key": self.item_header("alpha")
            + '[[items.checks]]\nargv = ["python3"]\nshell = true\n',
            "missing prompt": self.item_header("ghost") + valid_check,
            "unknown harness": self.item_header("alpha").replace(
                'harness = "codex"', 'harness = "gpt"'
            )
            + valid_check,
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.write_backlog(body)
                with self.assertRaises(devin_factory.FactoryError):
                    devin_factory.load_backlog(self.backlog)

    def test_rejects_duplicate_dedupe_keys_and_receipts(self) -> None:
        self.write_prompt("alpha")
        self.write_prompt("beta")
        check = '[[items.checks]]\nargv = ["python3", "-c", "pass"]\n'
        with self.subTest(case="duplicate dedupe_key"):
            self.write_backlog(
                self.item_header("alpha") + check + self.item_header("alpha") + check
            )
            with self.assertRaisesRegex(devin_factory.FactoryError, "factory_dedupe_key_duplicate"):
                devin_factory.load_backlog(self.backlog)
        with self.subTest(case="duplicate receipt path"):
            self.write_backlog(
                self.item_header("alpha", receipt=".orchestrator/factory/r/same.json")
                + check
                + self.item_header("beta", receipt=".orchestrator/factory/r/same.json")
                + check
            )
            with self.assertRaisesRegex(devin_factory.FactoryError, "factory_receipt_duplicate"):
                devin_factory.load_backlog(self.backlog)


class FactoryLifecycleTests(BacklogFixture):
    def coordinator(self) -> tuple[object, dict[str, object]]:
        coordinator, items = devin_factory._build_coordinator(self.workflow, self.backlog)
        return coordinator, items

    def intake(self, coordinator, items: dict[str, object]) -> list[bool]:
        coordinator.initialize()
        created: list[bool] = []
        for item in items.values():
            _, was_created, _ = coordinator.enqueue_prompt_file(
                harness=item.harness,
                title=item.title,
                prompt_file=item.prompt_file,
                dedupe_key=item.dedupe_key,
                placement=PlacementTarget.PANE,
                receipt=TaskReceipt(ReceiptKind.FILE, item.receipt),
                max_attempts=item.max_attempts,
            )
            created.append(was_created)
        return created

    def test_intake_is_idempotent_and_detects_contract_drift(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        self.write_backlog(self.item_toml("alpha", '["python3", "-c", "pass"]'))
        coordinator, items = self.coordinator()

        first = self.intake(coordinator, items)
        second = self.intake(coordinator, items)

        self.assertEqual(first, [True])
        self.assertEqual(second, [False])
        job = self.jobs()[0]
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["dedupe_key"], "alpha")
        self.assertEqual(job["receipt_kind"], "file")
        self.assertEqual(job["completion_policy"], "receipt-v1")

        self.write_prompt("alpha", "changed contract")
        with self.assertRaisesRegex(StoreError, "dedupe_contract_conflict"):
            self.intake(coordinator, devin_factory.load_backlog(self.backlog))

    def test_run_executes_checks_and_verifies_file_receipt(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        argv = json.dumps([sys.executable, "-c", "print('ok')"])
        self.write_backlog(self.item_toml("alpha", argv))
        coordinator, items = self.coordinator()
        self.intake(coordinator, items)

        result = coordinator.run_until_idle(timeout_seconds=30)

        self.assertTrue(result["idle"])
        job = self.jobs()[0]
        self.assertEqual(job["state"], JobState.SUCCEEDED.value)
        self.assertTrue(job["task_verified"])
        self.assertEqual(job["verification_class"], "verified")
        self.assertEqual(job["attempt_phase"], "outcome_committed")
        receipt = self.root / ".orchestrator/factory/receipts/alpha.json"
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(payload["dedupe_key"], "alpha")
        evidence = list((self.root / ".orchestrator/factory/evidence/alpha").glob("*.json"))
        self.assertEqual(len(evidence), 1)
        record = json.loads(evidence[0].read_text(encoding="utf-8"))
        self.assertTrue(record["verified"])
        self.assertEqual(record["checks"][0]["exit_code"], 0)

    def test_failed_check_propagates_without_recording_success(self) -> None:
        self.write_workflow(max_attempts=2)
        self.write_prompt("alpha")
        argv = json.dumps([sys.executable, "-c", "raise SystemExit(3)"])
        self.write_backlog(self.item_toml("alpha", argv))
        coordinator, items = self.coordinator()
        self.intake(coordinator, items)

        result = coordinator.run_until_idle(timeout_seconds=30)

        self.assertTrue(result["idle"])
        job = self.jobs()[0]
        self.assertEqual(job["state"], JobState.FAILED.value)
        self.assertEqual(job["attempts"], 2)
        self.assertEqual(job["error_code"], "factory_check_failed")
        self.assertFalse(job["task_verified"])
        self.assertFalse((self.root / ".orchestrator/factory/receipts/alpha.json").exists())
        with closing(sqlite3.connect(self.store.path)) as connection:
            rows = connection.execute(
                "SELECT state, task_verified, error_code FROM receipts "
                "WHERE job_id = ? ORDER BY id",
                (job["id"],),
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("pending", 0, "factory_check_failed"),
                ("failed", 0, "factory_check_failed"),
            ],
        )

    def test_unknown_backlog_item_fails_closed(self) -> None:
        self.write_workflow(max_attempts=1)
        self.backlog.write_text("schema_version = 1\nitems = []\n", encoding="utf-8")
        coordinator, _ = self.coordinator()
        coordinator.initialize()
        job_id, _ = self.store.enqueue(
            NewJob(
                workflow="factory-test",
                workspace=str(self.root),
                title="untracked",
                harness=Harness.CODEX,
                prompt="not in backlog",
                dedupe_key="untracked-v1",
                max_attempts=1,
                placement=PlacementTarget.PANE,
                receipt=TaskReceipt(
                    ReceiptKind.FILE, ".orchestrator/factory/receipts/untracked.json"
                ),
            )
        )

        result = coordinator.run_until_idle(timeout_seconds=30)

        self.assertTrue(result["idle"])
        job = self.jobs()[0]
        self.assertEqual(job["id"], job_id)
        self.assertEqual(job["state"], JobState.FAILED.value)
        self.assertEqual(job["error_code"], "factory_item_unknown")

    def test_retry_after_fix_turns_failure_into_verified_success(self) -> None:
        self.write_workflow(max_attempts=1)
        self.write_prompt("alpha")
        gate = self.root / "flag"
        probe = "import os,sys;" f"sys.exit(0 if os.path.exists({str(gate)!r}) else 5)"
        argv = json.dumps([sys.executable, "-c", probe])
        self.write_backlog(self.item_toml("alpha", argv))
        coordinator, items = self.coordinator()
        self.intake(coordinator, items)

        coordinator.run_until_idle(timeout_seconds=30)
        job = self.jobs()[0]
        self.assertEqual(job["state"], JobState.FAILED.value)

        gate.write_text("ok", encoding="utf-8")
        retried = self.store.retry_failed(
            "factory-test", job["id"], extra_attempts=1, workspace=str(self.root)
        )
        self.assertEqual(retried["state"], "pending")
        result = coordinator.run_until_idle(timeout_seconds=30)

        self.assertTrue(result["idle"])
        job = self.jobs()[0]
        self.assertEqual(job["state"], JobState.SUCCEEDED.value)
        self.assertTrue(job["task_verified"])

    def test_check_timeout_is_a_distinct_stable_error(self) -> None:
        self.write_workflow(max_attempts=1)
        self.write_prompt("alpha")
        argv = json.dumps([sys.executable, "-c", "import time;time.sleep(30)"])
        self.write_backlog(self.item_toml("alpha", argv, timeout=1))
        coordinator, items = self.coordinator()
        self.intake(coordinator, items)

        coordinator.run_until_idle(timeout_seconds=30)

        job = self.jobs()[0]
        self.assertEqual(job["state"], JobState.FAILED.value)
        self.assertEqual(job["error_code"], "factory_check_timeout")

    def test_checks_short_circuit_after_first_failure(self) -> None:
        self.write_workflow(max_attempts=1)
        self.write_prompt("alpha")
        marker = self.root / "marker"
        first = json.dumps([sys.executable, "-c", "raise SystemExit(3)"])
        second = json.dumps([sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"])
        self.write_backlog(
            self.item_header("alpha")
            + "[[items.checks]]\n"
            + f"argv = {first}\n"
            + "timeout_seconds = 60\n"
            + "[[items.checks]]\n"
            + f"argv = {second}\n"
            + "timeout_seconds = 60\n"
        )
        coordinator, items = self.coordinator()
        self.intake(coordinator, items)

        coordinator.run_until_idle(timeout_seconds=30)

        job = self.jobs()[0]
        self.assertEqual(job["state"], JobState.FAILED.value)
        self.assertEqual(job["error_code"], "factory_check_failed")
        self.assertFalse(marker.exists())
        evidence = sorted((self.root / ".orchestrator/factory/evidence/alpha").glob("*.json"))
        record = json.loads(evidence[-1].read_text(encoding="utf-8"))
        self.assertEqual(len(record["checks"]), 1)
        self.assertEqual(record["checks_skipped"], 1)

    def test_evidence_is_bounded_per_item(self) -> None:
        self.write_workflow()
        dispatcher = devin_factory.LocalDispatcher(
            workspace=self.root,
            items={},
            evidence_root=self.root / ".orchestrator/factory/evidence",
        )
        directory = self.root / ".orchestrator/factory/evidence/alpha"
        directory.mkdir(parents=True)
        for index in range(30):
            (directory / f"2020-01-01T00-00-{index:06d}Z-0000000{index % 10}.json").write_text(
                "{}", encoding="utf-8"
            )

        dispatcher._write_evidence(
            None,
            {"dedupe_key": "alpha", "correlation_id": "newest"},
            verified=False,
        )

        remaining = sorted(directory.glob("*.json"))
        self.assertEqual(len(remaining), devin_factory.EVIDENCE_KEEP_PER_ITEM)
        self.assertTrue(remaining[-1].name.endswith("-newest.json"))
        self.assertFalse((directory / "2020-01-01T00-00-000000Z-00000000.json").exists())

    def test_requires_gates_intake_until_dependency_succeeds(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        self.write_prompt("beta")
        check = json.dumps([sys.executable, "-c", "pass"])
        self.write_backlog(
            self.item_toml("alpha", check) + self.item_toml("beta", check, requires=["alpha"])
        )
        coordinator, items = self.coordinator()
        coordinator.initialize()
        args = devin_factory.build_parser().parse_args(
            [
                "--workflow",
                str(self.workflow),
                "--backlog",
                str(self.backlog),
                "intake",
            ]
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(devin_factory._command_intake(args), 0)
        first = json.loads(buffer.getvalue())
        self.assertEqual(first["added"], 1)
        self.assertEqual(first["waiting"], [{"dedupe_key": "beta", "requires": ["alpha"]}])
        self.assertEqual(len(self.jobs()), 1)

        coordinator.run_until_idle(timeout_seconds=30)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(devin_factory._command_intake(args), 0)
        second = json.loads(buffer.getvalue())
        self.assertEqual(second["added"], 1)
        self.assertEqual(second["waiting"], [])

        coordinator.run_until_idle(timeout_seconds=30)
        states = {job["dedupe_key"]: job["state"] for job in self.jobs()}
        self.assertEqual(states, {"alpha": "succeeded", "beta": "succeeded"})

    def test_load_backlog_rejects_requires_unknown_cycle_and_duplicate(self) -> None:
        check = '["python3", "-c", "pass"]'
        cases = {
            "unknown": self.item_toml("alpha", check, requires=["ghost"]),
            "self": self.item_toml("alpha", check, requires=["alpha"]),
            "cycle": (
                self.item_toml("alpha", check, requires=["beta"])
                + self.item_toml("beta", check, requires=["alpha"])
            ),
            "duplicate": self.item_toml("alpha", check, requires=["beta", "beta"]),
        }
        codes = {
            "unknown": "factory_requires_unknown",
            "self": "factory_requires_cycle",
            "cycle": "factory_requires_cycle",
            "duplicate": "factory_requires_duplicate",
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.write_prompt("alpha")
                self.write_prompt("beta")
                self.write_backlog(body)
                with self.assertRaisesRegex(devin_factory.FactoryError, codes[name]):
                    devin_factory.load_backlog(self.backlog)

    def test_unwritable_receipt_path_fails_closed_with_stable_code(self) -> None:
        self.write_workflow(max_attempts=1)
        self.write_prompt("alpha")
        self.write_backlog(self.item_toml("alpha", json.dumps([sys.executable, "-c", "pass"])))
        receipt_dir = self.root / ".orchestrator/factory/receipts"
        receipt_dir.mkdir(parents=True)
        receipt_dir.chmod(0o555)
        self.addCleanup(receipt_dir.chmod, 0o755)
        coordinator, items = self.coordinator()
        self.intake(coordinator, items)

        coordinator.run_until_idle(timeout_seconds=30)

        job = self.jobs()[0]
        self.assertEqual(job["state"], JobState.FAILED.value)
        self.assertEqual(job["error_code"], "factory_receipt_write_failed")
        self.assertFalse(job["task_verified"])

    def test_item_max_attempts_overrides_workflow_default(self) -> None:
        self.write_workflow(max_attempts=3)
        self.write_prompt("alpha")
        self.write_backlog(
            self.item_toml(
                "alpha",
                json.dumps([sys.executable, "-c", "raise SystemExit(3)"]),
                max_attempts=1,
            )
        )
        coordinator, items = self.coordinator()
        self.intake(coordinator, items)

        coordinator.run_until_idle(timeout_seconds=30)

        job = self.jobs()[0]
        self.assertEqual(job["state"], JobState.FAILED.value)
        self.assertEqual(job["max_attempts"], 1)
        self.assertEqual(job["attempts"], 1)

    def test_item_max_attempts_invalid_is_rejected(self) -> None:
        self.write_prompt("alpha")
        for bad in ("0", "9", '"one"'):
            with self.subTest(max_attempts=bad):
                self.write_backlog(
                    self.item_header("alpha")
                    + f"max_attempts = {bad}\n"
                    + '[[items.checks]]\nargv = ["python3", "-c", "pass"]\n'
                )
                with self.assertRaisesRegex(
                    devin_factory.FactoryError, "factory_max_attempts_invalid"
                ):
                    devin_factory.load_backlog(self.backlog)

    def test_run_once_reports_batch_counts(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        argv = json.dumps([sys.executable, "-c", "pass"])
        self.write_backlog(self.item_toml("alpha", argv))
        coordinator, items = self.coordinator()
        self.intake(coordinator, items)

        report = coordinator.run_once()

        self.assertEqual(report["claimed"], 1)
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(report["queue"]["succeeded"], 1)

    def test_harness_outside_workflow_workers_is_rejected(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        self.write_backlog(
            self.item_toml("alpha", '["python3", "-c", "pass"]').replace(
                'harness = "codex"', 'harness = "claude"'
            )
        )
        with self.assertRaisesRegex(devin_factory.FactoryError, "factory_harness_has_no_worker"):
            self.coordinator()


class FactoryCliTests(BacklogFixture):
    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        source = str(REPO_ROOT / "src")
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = source if not existing else source + os.pathsep + existing
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--workflow",
                str(self.workflow),
                "--backlog",
                str(self.backlog),
                *argv,
            ],
            capture_output=True,
            text=True,
            cwd=self.root,
            env=environment,
            check=False,
        )

    def test_cli_intake_run_status_report_end_to_end(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        self.write_backlog(self.item_toml("alpha", json.dumps([sys.executable, "-c", "pass"])))

        intake = self.run_cli("intake")
        self.assertEqual(intake.returncode, 0, intake.stderr)
        self.assertEqual(json.loads(intake.stdout)["added"], 1)

        run = self.run_cli("run")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["queue"]["succeeded"], 1)

        status = self.run_cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["counts"]["succeeded"], 1)
        self.assertEqual(payload["jobs"][0]["dedupe_key"], "alpha")

        report = self.run_cli("report")
        self.assertEqual(report.returncode, 0, report.stderr)
        rendered = (self.root / ".orchestrator/factory/report.md").read_text(encoding="utf-8")
        self.assertIn("alpha", rendered)
        self.assertIn("succeeded", rendered)

    def test_cli_run_exit_code_reflects_queue_failure(self) -> None:
        self.write_workflow(max_attempts=1)
        self.write_prompt("alpha")
        self.write_backlog(
            self.item_toml("alpha", json.dumps([sys.executable, "-c", "raise SystemExit(3)"]))
        )
        self.assertEqual(self.run_cli("intake").returncode, 0)

        run = self.run_cli("run")

        self.assertEqual(run.returncode, 1)
        job = self.jobs()[0]
        self.assertEqual(job["state"], JobState.FAILED.value)

    def test_cli_run_on_empty_queue_is_idle_success(self) -> None:
        self.write_workflow()
        self.backlog.write_text("schema_version = 1\nitems = []\n", encoding="utf-8")

        run = self.run_cli("run")

        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertTrue(payload["idle"])
        self.assertEqual(payload["claimed"], 0)

    def test_cli_status_includes_error_summary(self) -> None:
        self.write_workflow(max_attempts=1)
        self.write_prompt("alpha")
        self.write_backlog(
            self.item_toml("alpha", json.dumps([sys.executable, "-c", "raise SystemExit(3)"]))
        )
        self.run_cli("intake")
        self.run_cli("run")

        status = self.run_cli("status")

        self.assertEqual(status.returncode, 0, status.stderr)
        job = json.loads(status.stdout)["jobs"][0]
        self.assertEqual(job["error_code"], "factory_check_failed")
        self.assertIn("exit=3", job["error_summary"])

    def test_cli_status_surfaces_backlog_error_not_silence(self) -> None:
        self.write_workflow()
        self.backlog.write_text("schema_version = 2\nitems = []\n", encoding="utf-8")

        status = self.run_cli("status")

        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertIn("factory_backlog_schema_version", payload["backlog"]["error"])

    def test_cli_concurrent_intake_is_idempotent(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        self.write_backlog(self.item_toml("alpha", json.dumps([sys.executable, "-c", "pass"])))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_cli("intake"), range(2)))

        self.assertTrue(all(result.returncode == 0 for result in results))
        added = sum(json.loads(result.stdout)["added"] for result in results)
        self.assertEqual(added, 1)
        self.assertEqual(len(self.jobs()), 1)

    def test_cli_rejects_invalid_backlog_and_missing_workflow(self) -> None:
        self.write_workflow()
        self.backlog.write_text("not toml [", encoding="utf-8")
        result = self.run_cli("validate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("factory_backlog_invalid", result.stderr)

        self.write_backlog(self.item_toml("alpha", '["python3", "-c", "pass"]'))
        self.workflow.unlink()
        result = self.run_cli("validate")
        self.assertEqual(result.returncode, 2)


class FactoryValidateTests(BacklogFixture):
    def parse(self, *extra: str):
        return devin_factory.build_parser().parse_args(
            [
                "--workflow",
                str(self.workflow),
                "--backlog",
                str(self.backlog),
                "validate",
                *extra,
            ]
        )

    def capture(self, *extra: str) -> dict[str, object]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = devin_factory._command_validate(self.parse(*extra))
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())

    def test_validate_lists_items_and_queue_coverage(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        self.write_prompt("beta")
        check = '["python3", "-c", "pass"]'
        self.write_backlog(self.item_toml("alpha", check) + self.item_toml("beta", check))
        coordinator, items = devin_factory._build_coordinator(self.workflow, self.backlog)
        coordinator.initialize()
        coordinator.enqueue_prompt_file(
            harness=items["alpha"].harness,
            title=items["alpha"].title,
            prompt_file=items["alpha"].prompt_file,
            dedupe_key="alpha",
            placement=PlacementTarget.PANE,
            receipt=TaskReceipt(ReceiptKind.FILE, items["alpha"].receipt),
        )

        payload = self.capture()

        self.assertTrue(payload["valid"])
        self.assertTrue(payload["state_db"])
        by_key = {item["dedupe_key"]: item for item in payload["items"]}
        self.assertTrue(by_key["alpha"]["queued"])
        self.assertFalse(by_key["beta"]["queued"])
        self.assertTrue(by_key["alpha"]["harness_supported"])
        self.assertEqual(by_key["alpha"]["checks"], [["python3", "-c", "pass"]])

    def test_validate_reports_absent_state_db_without_creating_it(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        self.write_backlog(self.item_toml("alpha", '["python3", "-c", "pass"]'))
        state_db = self.root / "state.db"
        state_db.unlink()

        payload = self.capture()

        self.assertFalse(payload["state_db"])
        self.assertFalse(payload["items"][0]["queued"])
        self.assertFalse(state_db.exists())

    def test_validate_tolerates_unreadable_state_db(self) -> None:
        self.write_workflow()
        self.write_prompt("alpha")
        self.write_backlog(self.item_toml("alpha", '["python3", "-c", "pass"]'))
        state_db = self.root / "state.db"
        state_db.write_text("not a database", encoding="utf-8")

        payload = self.capture()

        self.assertTrue(payload["state_db"])
        self.assertIsNotNone(payload["state_db_error"])
        self.assertFalse(payload["items"][0]["queued"])

    def test_validate_rejects_invalid_backlog_via_main(self) -> None:
        self.write_workflow()
        self.backlog.write_text("schema_version = 2\nitems = []\n", encoding="utf-8")

        with redirect_stderr(io.StringIO()):
            code = devin_factory.main(
                [
                    "--workflow",
                    str(self.workflow),
                    "--backlog",
                    str(self.backlog),
                    "validate",
                ]
            )

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
