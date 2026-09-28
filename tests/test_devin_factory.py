from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
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

    def item_header(self, dedupe_key: str, *, receipt: str | None = None) -> str:
        receipt_line = (
            f'receipt = "{receipt}"\n'
            if receipt is not None
            else f'receipt = ".orchestrator/factory/receipts/{dedupe_key}.json"\n'
        )
        return (
            "[[items]]\n"
            f'dedupe_key = "{dedupe_key}"\n'
            f'title = "item {dedupe_key}"\n'
            'harness = "codex"\n'
            f'prompt_file = "prompts/{dedupe_key}.md"\n'
            f"{receipt_line}"
        )

    def item_toml(
        self,
        dedupe_key: str,
        argv: str,
        *,
        timeout: int = 60,
        receipt: str | None = None,
    ) -> str:
        return (
            self.item_header(dedupe_key, receipt=receipt)
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


if __name__ == "__main__":
    unittest.main()
