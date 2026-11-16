from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from test_distribution import CLI, REPO_ROOT, DistributionCliFixture

from herdr_orchestrator import __version__


class DistributionJournalTests(DistributionCliFixture, unittest.TestCase):
    def test_malformed_installer_journal_fails_closed_for_every_reader(self) -> None:
        def mixed_modes(journal: bytes) -> bytes:
            payload = json.loads(journal)
            for inventory_name in ("prior_inventory", "desired_inventory"):
                for item in payload[inventory_name].values():
                    item["state"].pop("mode", None)
            for operation in payload["operations"]:
                operation["original"].pop("mode", None)
                operation["desired"].pop("mode", None)
            next(
                operation
                for operation in payload["operations"]
                if operation["desired"]["kind"] == "regular"
            )["desired"]["mode"] = 0o777
            return f"{json.dumps(payload, indent=2)}\n".encode()

        corruptions = {
            "invalid-json": lambda journal: b"{",
            "invalid-utf8": lambda journal: journal + b"\xff",
            "mixed-modes": mixed_modes,
            "digest-mismatch": lambda journal: (
                lambda payload: f"{json.dumps(payload, indent=2)}\n".encode()
            )(
                {
                    **json.loads(journal),
                    "operations": [
                        {
                            **json.loads(journal)["operations"][0],
                            "desired_content_base64": "dGFtcGVyZWQ=",
                        },
                        *json.loads(journal)["operations"][1:],
                    ],
                }
            ),
            "unsupported-harness": lambda journal: (
                lambda payload: f"{json.dumps(payload, indent=2)}\n".encode()
            )(
                {
                    **json.loads(journal),
                    "harnesses": ["not-a-harness"],
                }
            ),
        }
        commands = {
            "doctor": ["doctor"],
            "install": ["install", "--harness", "droid"],
            "uninstall": ["uninstall"],
            "upgrade": ["upgrade", "--harness", "droid"],
        }
        for corruption_name, corrupt in corruptions.items():
            for command_name, arguments in commands.items():
                with (
                    self.subTest(
                        corruption=corruption_name,
                        command=command_name,
                    ),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    project = Path(temporary)
                    (project / ".git").mkdir()
                    environment = os.environ.copy()
                    environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL"] = "journal:published"
                    interrupted = self._run(
                        "install",
                        "--project",
                        str(project),
                        "--harness",
                        "droid",
                        env=environment,
                    )
                    self.assertEqual(
                        interrupted.returncode,
                        86,
                        interrupted.stderr,
                    )
                    journal_path = project / ".herdr-orchestrator/install-journal.json"
                    corrupted = corrupt(journal_path.read_bytes())
                    journal_path.write_bytes(corrupted)

                    result = self._run(
                        *arguments,
                        "--project",
                        str(project),
                    )

                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(
                        result.stderr.strip(),
                        "installer_journal_invalid",
                    )
                    self.assertEqual(journal_path.read_bytes(), corrupted)

    def test_install_recovers_after_interruption_at_journal_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            interrupted_environment = os.environ.copy()
            interrupted_environment.update(
                {
                    "NODE_ENV": "test",
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": "journal:published",
                }
            )

            interrupted = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                env=interrupted_environment,
            )

            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            journal_path = project / ".herdr-orchestrator/install-journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(journal["schema_version"], 1)
            self.assertEqual(journal["package"], "herdr-orchestrator")
            self.assertRegex(
                journal["transaction_id"],
                r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-" r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            )
            self.assertEqual(journal["package_version"], __version__)
            self.assertEqual(journal["harnesses"], ["droid"])
            self.assertTrue(journal["install_skill"])
            self.assertGreater(len(journal["prior_inventory"]), 0)
            self.assertEqual(
                set(journal["prior_inventory"]),
                set(journal["desired_inventory"]),
            )
            self.assertGreater(len(journal["operations"]), 0)
            self.assertEqual(journal["progress"]["completed_operations"], 0)
            owner_claims = list(journal_path.parent.glob(".install-journal.*.owner"))
            self.assertEqual(len(owner_claims), 1)
            self.assertFalse(
                (project / ".herdr-orchestrator/workflows/multi-harness.toml").exists()
            )

            resumed = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertFalse(journal_path.exists())
            self.assertEqual(
                list(journal_path.parent.glob(".install-journal.*.owner")),
                [],
            )
            self.assertTrue(
                (project / ".herdr-orchestrator/workflows/multi-harness.toml").is_file()
            )
            self.assertTrue((project / ".herdr-orchestrator/manifest.json").is_file())

    def test_uninstall_reconciles_an_interrupted_install_before_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            interrupted_environment = os.environ.copy()
            interrupted_environment.update(
                {
                    "NODE_ENV": "test",
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": "journal:published",
                }
            )
            interrupted = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                env=interrupted_environment,
            )
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)

            uninstall = self._run("uninstall", "--project", str(project))

            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertFalse((project / ".herdr-orchestrator/install-journal.json").exists())
            self.assertFalse((project / ".herdr-orchestrator/manifest.json").exists())
            self.assertFalse(
                (project / ".herdr-orchestrator/workflows/multi-harness.toml").exists()
            )

    def test_uninstall_recovers_after_interruption_at_journal_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
            manifest = project / ".herdr-orchestrator/manifest.json"
            interrupted_environment = os.environ.copy()
            interrupted_environment.update(
                {
                    "NODE_ENV": "test",
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": "journal:published",
                }
            )

            interrupted = self._run(
                "uninstall",
                "--project",
                str(project),
                env=interrupted_environment,
            )

            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            journal_path = project / ".herdr-orchestrator/install-journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(journal["command"], "uninstall")
            self.assertEqual(journal["harnesses"], ["droid"])
            self.assertTrue(workflow.is_file())
            self.assertTrue(manifest.is_file())

            resumed = self._run("uninstall", "--project", str(project))

            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertFalse(journal_path.exists())
            self.assertFalse(workflow.exists())
            self.assertFalse(manifest.exists())

    def test_uninstall_is_idempotent_after_journal_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            first = self._run("uninstall", "--project", str(project))
            self.assertEqual(first.returncode, 0, first.stderr)

            second = self._run("uninstall", "--project", str(project))

            self.assertEqual(second.returncode, 0, second.stderr)
            payload = json.loads(second.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["preserved"], [])
            self.assertFalse((project / ".herdr-orchestrator/manifest.json").exists())

    def test_doctor_reports_an_active_journal_without_replaying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            interrupted_environment = os.environ.copy()
            interrupted_environment.update(
                {
                    "NODE_ENV": "test",
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": "journal:published",
                }
            )
            interrupted = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                env=interrupted_environment,
            )
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            journal_path = project / ".herdr-orchestrator/install-journal.json"
            before = journal_path.read_bytes()

            doctor_environment = os.environ.copy()
            doctor_environment["PYTHON"] = "/bin/false"
            doctor = self._run(
                "doctor",
                "--project",
                str(project),
                env=doctor_environment,
            )

            self.assertEqual(doctor.returncode, 1, doctor.stderr)
            installation = json.loads(doctor.stdout)["installation"]
            self.assertFalse(installation["ok"])
            self.assertFalse(installation["manifest"])
            self.assertTrue(installation["journal"]["active"])
            self.assertEqual(installation["journal"]["command"], "install")
            self.assertEqual(installation["journal"]["conflicts"], [])
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                installation["package_missing"],
            )
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                installation["manifest_missing"],
            )
            self.assertEqual(journal_path.read_bytes(), before)
            self.assertFalse(
                (project / ".herdr-orchestrator/workflows/multi-harness.toml").exists()
            )

    def test_doctor_uses_an_active_upgrade_selection_for_package_comparison(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            installed = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            environment = os.environ.copy()
            environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL"] = "journal:published"
            interrupted = self._run(
                "upgrade",
                "--project",
                str(project),
                "--harness",
                "droid",
                "--harness",
                "codex",
                env=environment,
            )
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)

            doctor_environment = os.environ.copy()
            doctor_environment["PYTHON"] = "/bin/false"
            doctor = self._run(
                "doctor",
                "--project",
                str(project),
                env=doctor_environment,
            )

            self.assertEqual(doctor.returncode, 1, doctor.stderr)
            installation = json.loads(doctor.stdout)["installation"]
            self.assertEqual(
                installation["journal"]["harnesses"],
                ["droid", "codex"],
            )
            self.assertEqual(
                installation["package_missing"],
                [
                    ".herdr-orchestrator/profiles/harnesses/codex.md",
                    ".herdr-orchestrator/profiles/harnesses/codex.toml",
                ],
            )
            self.assertEqual(
                installation["manifest_missing"],
                [
                    ".herdr-orchestrator/profiles/harnesses/codex.md",
                    ".herdr-orchestrator/profiles/harnesses/codex.toml",
                ],
            )

    def test_doctor_reports_a_corrupt_durable_operation_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            environment = os.environ.copy()
            environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX"] = (
                "temporary:target:operation-1:"
            )
            interrupted = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                env=environment,
            )
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            journal_path = project / ".herdr-orchestrator/install-journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            operation = journal["operations"][0]
            relative_path = operation["target"]["path"]
            target = project / relative_path
            operation_temporary = target.with_name(
                f".{target.name}.herdr-{journal['transaction_id']}-" f"{operation['id']}.tmp"
            )
            operation_temporary.write_bytes(b"corrupt durable temporary\n")

            doctor = self._run("doctor", "--project", str(project))

            self.assertEqual(doctor.returncode, 1, doctor.stderr)
            installation = json.loads(doctor.stdout)["installation"]
            conflict = f"temporary:{relative_path}"
            self.assertIn(conflict, installation["journal"]["conflicts"])
            recovered = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(recovered.returncode, 2)
            self.assertEqual(
                recovered.stderr.strip(),
                f"installer_recovery_conflict: {conflict}",
            )

    def test_recovery_conflict_preserves_the_journal_and_user_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            interrupted_environment = os.environ.copy()
            interrupted_environment.update(
                {
                    "NODE_ENV": "test",
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": "journal:published",
                }
            )
            interrupted = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                env=interrupted_environment,
            )
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            journal_path = project / ".herdr-orchestrator/install-journal.json"
            journal_before = journal_path.read_bytes()
            journal = json.loads(journal_before)
            operation = next(
                item for item in journal["operations"] if item["target"]["scope"] == "project"
            )
            relative_path = operation["target"]["path"]
            conflicted_path = project / relative_path
            conflicted_path.parent.mkdir(parents=True, exist_ok=True)
            conflicted_path.write_bytes(b"user bytes outside both journal endpoints\n")

            resumed = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(resumed.returncode, 2)
            self.assertEqual(
                resumed.stderr.strip(),
                f"installer_recovery_conflict: {relative_path}",
            )
            self.assertEqual(
                conflicted_path.read_bytes(),
                b"user bytes outside both journal endpoints\n",
            )
            self.assertEqual(journal_path.read_bytes(), journal_before)
            self.assertFalse((project / ".herdr-orchestrator/manifest.json").exists())

    def test_install_rechecks_desired_bytes_before_manifest_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "NODE_ENV": "test",
                    "HERDR_ORCHESTRATOR_TEST_REWRITE_AT_LABEL_PREFIX": "target:operation-1:",
                }
            )

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                env=environment,
            )

            journal_path = project / ".herdr-orchestrator/install-journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            first_operation = journal["operations"][0]
            relative_path = first_operation["target"]["path"]
            target = project / relative_path
            self.assertEqual(install.returncode, 2)
            self.assertEqual(
                install.stderr.strip(),
                f"installer_recovery_conflict: {relative_path}",
            )
            self.assertEqual(
                target.read_bytes(),
                b"installer test user edit after durable mutation\n",
            )
            self.assertTrue(journal_path.is_file())
            self.assertFalse((project / ".herdr-orchestrator/manifest.json").exists())

    def test_concurrent_fresh_installs_have_one_exclusive_journal_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            barrier = root / "barrier"
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(project)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            environment = os.environ.copy()
            environment.update(
                {
                    "NODE_ENV": "test",
                    "HERDR_ORCHESTRATOR_TEST_JOURNAL_CLAIM_BARRIER": str(barrier),
                }
            )
            harnesses = ["droid", "codex"]
            processes = [
                subprocess.Popen(
                    [
                        *self._node_command(CLI, environment),
                        "install",
                        "--project",
                        str(project),
                        "--harness",
                        harness,
                    ],
                    cwd=REPO_ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for harness in harnesses
            ]
            deadline = time.monotonic() + 5
            while len(list(barrier.glob("*.ready"))) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            ready_count = len(list(barrier.glob("*.ready")))
            barrier.mkdir(parents=True, exist_ok=True)
            (barrier / "release").write_text("", encoding="utf-8")
            outputs = [process.communicate(timeout=30) for process in processes]

            self.assertEqual(ready_count, 2)
            returncodes = [process.returncode for process in processes]
            self.assertEqual(sorted(returncodes), [0, 2], outputs)
            winner_index = returncodes.index(0)
            loser_index = returncodes.index(2)
            self.assertEqual(
                outputs[loser_index][1].strip(),
                "installer_transaction_active",
            )
            manifest = json.loads(
                (project / ".herdr-orchestrator/manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["harnesses"], [harnesses[winner_index]])
            profiles = project / ".herdr-orchestrator/profiles/harnesses"
            self.assertTrue((profiles / f"{harnesses[winner_index]}.toml").is_file())
            self.assertFalse((profiles / f"{harnesses[loser_index]}.toml").exists())
            self.assertFalse((project / ".herdr-orchestrator/install-journal.json").exists())
            self.assertEqual(
                list((project / ".herdr-orchestrator").rglob("*.tmp")),
                [],
            )

    def test_git_exclude_preserves_non_utf8_bytes_through_recovery_and_uninstall(
        self,
    ) -> None:
        for suffix in (b"\xffcaller-owned\n", b"\xffcaller-owned"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                initialized = subprocess.run(
                    ["git", "init", "--quiet", str(project)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(initialized.returncode, 0, initialized.stderr)
                exclude = project / ".git/info/exclude"
                with exclude.open("ab") as stream:
                    stream.write(suffix)
                exclude.chmod(0o600)
                original = exclude.read_bytes()
                interrupted_environment = os.environ.copy()
                interrupted_environment.update(
                    {
                        "NODE_ENV": "test",
                        "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": "journal:published",
                    }
                )
                interrupted = self._run(
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "droid",
                    env=interrupted_environment,
                )
                self.assertEqual(interrupted.returncode, 86, interrupted.stderr)

                recovered = self._run(
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "droid",
                )

                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertTrue(exclude.read_bytes().startswith(original))
                self.assertEqual(exclude.stat().st_mode & 0o777, 0o600)
                uninstall = self._run("uninstall", "--project", str(project))
                self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
                self.assertEqual(exclude.read_bytes(), original)
                self.assertEqual(exclude.stat().st_mode & 0o777, 0o600)

    def test_install_never_deletes_an_unowned_fixed_journal_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            caller_file = project / ".herdr-orchestrator/.install-journal.json.tmp"
            caller_file.parent.mkdir()
            caller_file.write_bytes(b"caller-owned temporary bytes\n")

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertEqual(
                caller_file.read_bytes(),
                b"caller-owned temporary bytes\n",
            )
            self.assertTrue((project / ".herdr-orchestrator/manifest.json").is_file())

    def test_noop_install_never_claims_a_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            installed = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            environment = os.environ.copy()
            environment["HERDR_ORCHESTRATOR_TEST_FAIL_ON_JOURNAL_CLAIM"] = "1"

            repeated = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                env=environment,
            )

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertFalse((project / ".herdr-orchestrator/install-journal.json").exists())

    @unittest.skipIf(os.geteuid() == 0, "permission errors require a non-root user")
    def test_upgrade_recovers_after_a_target_directory_write_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            profiles = project / ".herdr-orchestrator/profiles/harnesses"
            existing_profile = profiles / "droid.toml"
            existing_bytes = existing_profile.read_bytes()
            profiles.chmod(0o500)
            try:
                failed = self._run(
                    "upgrade",
                    "--project",
                    str(project),
                    "--harness",
                    "droid",
                    "--harness",
                    "codex",
                )
            finally:
                profiles.chmod(0o700)

            self.assertEqual(failed.returncode, 2)
            self.assertIn("EACCES", failed.stderr)
            journal = project / ".herdr-orchestrator/install-journal.json"
            self.assertTrue(journal.is_file())
            self.assertEqual(existing_profile.read_bytes(), existing_bytes)

            recovered = self._run(
                "upgrade",
                "--project",
                str(project),
                "--harness",
                "droid",
                "--harness",
                "codex",
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertFalse(journal.exists())
            self.assertTrue((profiles / "codex.toml").is_file())


if __name__ == "__main__":
    unittest.main()
