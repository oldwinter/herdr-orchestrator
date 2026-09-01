from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin/herdr-orchestrator.mjs"
INSTALLER_FAULT_LOADER = REPO_ROOT / "tests/installer_fault_loader.mjs"


class InstallerJournalBoundaryTests(unittest.TestCase):
    def test_mixed_published_and_owner_intents_are_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
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
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            directory = project / ".herdr-orchestrator"
            journal_path = directory / "install-journal.json"
            owner = next(directory.glob(".install-journal.*.owner"))
            owner_before = owner.read_bytes()
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            for inventory_name in ("prior_inventory", "desired_inventory"):
                for item in journal[inventory_name].values():
                    item["state"].pop("mode", None)
            for operation in journal["operations"]:
                operation["original"].pop("mode", None)
                operation["desired"].pop("mode", None)
            journal_path.unlink()
            journal_path.write_text(
                f"{json.dumps(journal, indent=2)}\n",
                encoding="utf-8",
            )
            journal_before = journal_path.read_bytes()

            doctor = self._run("doctor", "--project", str(project))
            self.assertEqual(doctor.returncode, 2)
            self.assertEqual(doctor.stderr.strip(), "installer_journal_invalid")
            recovered = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(recovered.returncode, 2)
            self.assertEqual(recovered.stderr.strip(), "installer_journal_invalid")
            self.assertEqual(owner.read_bytes(), owner_before)
            self.assertEqual(journal_path.read_bytes(), journal_before)
            self.assertFalse((project / ".herdr-orchestrator/manifest.json").exists())

    def test_partial_manifest_mode_map_is_rejected_without_mutation(self) -> None:
        for command, arguments in {
            "doctor": ["doctor"],
            "upgrade": ["upgrade", "--harness", "droid"],
            "uninstall": ["uninstall"],
        }.items():
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temporary:
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
                workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
                workflow.chmod(0o600)
                manifest_path = project / ".herdr-orchestrator/manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                del manifest["file_modes"][".herdr-orchestrator/workflows/multi-harness.toml"]
                manifest_path.write_text(
                    f"{json.dumps(manifest, indent=2)}\n",
                    encoding="utf-8",
                )

                result = self._run(
                    *arguments,
                    "--project",
                    str(project),
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("manifest_invalid", result.stderr)
                self.assertTrue(workflow.is_file())
                self.assertEqual(workflow.stat().st_mode & 0o777, 0o600)

    def test_install_after_uninstall_reinstalls_skill_with_only_empty_router_dirs(self) -> None:
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
            skill = project / ".agents/skills/herdr-orchestrator/SKILL.md"
            self.assertTrue(skill.is_file())

            uninstalled = self._run("uninstall", "--project", str(project))

            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertFalse(skill.exists())
            self.assertTrue((project / ".agents/skills").is_dir())
            self.assertTrue((project / ".agents/skills/herdr-orchestrator").is_dir())

            reinstalled = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr)
            self.assertTrue(skill.is_file())
            self.assertEqual(json.loads(reinstalled.stdout)["skill"], "managed")

    def test_malformed_owner_pid_returns_a_stable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
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
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            owner = next((project / ".herdr-orchestrator").glob(".install-journal.*.owner"))
            parts = owner.name.split(".")
            malformed = owner.with_name(
                ".".join(
                    [
                        "",
                        "install-journal",
                        parts[2],
                        "9" * 100,
                        parts[4],
                        "owner",
                    ]
                )
            )
            owner.rename(malformed)

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
            self.assertTrue(installation["journal"]["invalid"])
            self.assertTrue(installation["journal"]["conflicts"])

            contender = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(contender.returncode, 2)
            self.assertTrue(contender.stderr.startswith("installer_journal_owner_conflict:"))
            self.assertTrue(malformed.is_file())

    def test_mode_only_change_is_reported_and_uninstall_preserves_the_file(self) -> None:
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
            workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
            workflow.chmod(0o600)

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
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                installation["modified"],
            )
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                installation["package_modified"],
            )

            upgrade = self._run(
                "upgrade",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(upgrade.returncode, 1, upgrade.stderr)
            upgrade_payload = json.loads(upgrade.stdout)
            self.assertFalse(upgrade_payload["ok"])
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                upgrade_payload["preserved"],
            )
            self.assertTrue(workflow.is_file())
            self.assertEqual(workflow.stat().st_mode & 0o777, 0o600)

            uninstall = self._run(
                "uninstall",
                "--project",
                str(project),
            )

            self.assertEqual(uninstall.returncode, 1, uninstall.stderr)
            payload = json.loads(uninstall.stdout)
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                payload["preserved"],
            )
            self.assertTrue(workflow.is_file())
            self.assertEqual(workflow.stat().st_mode & 0o777, 0o600)

    def test_legacy_manifest_mode_only_change_is_reported_and_preserved(self) -> None:
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
            workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
            manifest_path = project / ".herdr-orchestrator/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("file_modes", None)
            manifest_path.write_text(
                f"{json.dumps(manifest, indent=2)}\n",
                encoding="utf-8",
            )
            workflow.chmod(0o600)

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
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                installation["modified"],
            )
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                installation["package_modified"],
            )
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                installation["mode_unverified"],
            )

            upgrade = self._run(
                "upgrade",
                "--project",
                str(project),
                "--harness",
                "droid",
                "--harness",
                "codex",
            )
            self.assertEqual(upgrade.returncode, 1, upgrade.stderr)
            upgrade_payload = json.loads(upgrade.stdout)
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                upgrade_payload["preserved"],
            )
            self.assertTrue(workflow.is_file())
            self.assertEqual(workflow.stat().st_mode & 0o777, 0o600)

            uninstall = self._run("uninstall", "--project", str(project))
            self.assertEqual(uninstall.returncode, 1, uninstall.stderr)
            uninstall_payload = json.loads(uninstall.stdout)
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                uninstall_payload["preserved"],
            )
            self.assertTrue(workflow.is_file())
            self.assertEqual(workflow.stat().st_mode & 0o777, 0o600)

    def test_legacy_uninstall_recovery_preserves_a_mode_only_edit(self) -> None:
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
            workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
            environment = os.environ.copy()
            environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL"] = "journal:published"
            interrupted = self._run("uninstall", "--project", str(project), env=environment)
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            journal_path = project / ".herdr-orchestrator/install-journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            for inventory_name in ("prior_inventory", "desired_inventory"):
                for item in journal[inventory_name].values():
                    item["state"].pop("mode", None)
            for operation in journal["operations"]:
                operation["original"].pop("mode", None)
                operation["desired"].pop("mode", None)
            journal_path.write_text(
                f"{json.dumps(journal, indent=2)}\n",
                encoding="utf-8",
            )
            workflow.chmod(0o600)

            doctor = self._run(
                "doctor",
                "--project",
                str(project),
                env={**os.environ, "PYTHON": "/bin/false"},
            )
            self.assertEqual(doctor.returncode, 1, doctor.stderr)
            installation = json.loads(doctor.stdout)["installation"]
            self.assertTrue(installation["journal"]["active"])
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                installation["journal"]["preserved"],
            )
            self.assertEqual(installation["journal"]["conflicts"], [])

            recovered = self._run("uninstall", "--project", str(project))
            self.assertEqual(recovered.returncode, 1, recovered.stderr)
            payload = json.loads(recovered.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                payload["preserved"],
            )
            self.assertTrue(workflow.is_file())
            self.assertEqual(workflow.stat().st_mode & 0o777, 0o600)
            self.assertFalse(journal_path.exists())
            self.assertFalse((project / ".herdr-orchestrator/manifest.json").exists())

    def test_legacy_manifest_never_infers_mode_across_umasks(self) -> None:
        previous_umask = os.umask(0)
        os.umask(previous_umask)
        current_default = 0o666 & ~previous_umask
        for command in ("doctor", "upgrade", "uninstall"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                (project / ".git").mkdir()
                restrictive_install = self._run(
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "droid",
                    preexec_fn=lambda: os.umask(0o077),
                )
                self.assertEqual(restrictive_install.returncode, 0, restrictive_install.stderr)
                manifest_path = project / ".herdr-orchestrator/manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.pop("file_modes", None)
                manifest_path.write_text(
                    f"{json.dumps(manifest, indent=2)}\n",
                    encoding="utf-8",
                )
                workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
                workflow.chmod(current_default)

                if command == "doctor":
                    result = self._run(
                        "doctor",
                        "--project",
                        str(project),
                        env={**os.environ, "PYTHON": "/bin/false"},
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    installation = json.loads(result.stdout)["installation"]
                    self.assertIn(
                        ".herdr-orchestrator/workflows/multi-harness.toml",
                        installation["mode_unverified"],
                    )
                    self.assertIn(
                        ".herdr-orchestrator/workflows/multi-harness.toml",
                        installation["modified"],
                    )
                elif command == "upgrade":
                    result = self._run(
                        "upgrade",
                        "--project",
                        str(project),
                        "--harness",
                        "droid",
                        "--harness",
                        "codex",
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(
                        ".herdr-orchestrator/workflows/multi-harness.toml",
                        json.loads(result.stdout)["preserved"],
                    )
                    self.assertTrue(workflow.is_file())
                else:
                    result = self._run("uninstall", "--project", str(project))
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(
                        ".herdr-orchestrator/workflows/multi-harness.toml",
                        json.loads(result.stdout)["preserved"],
                    )
                    self.assertTrue(workflow.is_file())
                self.assertEqual(workflow.stat().st_mode & 0o7777, current_default)

    def test_legacy_preserved_recovery_rechecks_after_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            barrier = root / "preserved-discovery-barrier"
            (project / ".git").mkdir(parents=True)
            installed = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            interrupted_environment = os.environ.copy()
            interrupted_environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL"] = (
                "journal:published"
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
            for inventory_name in ("prior_inventory", "desired_inventory"):
                for item in journal[inventory_name].values():
                    item["state"].pop("mode", None)
            for operation in journal["operations"]:
                operation["original"].pop("mode", None)
                operation["desired"].pop("mode", None)
            journal_path.write_text(
                f"{json.dumps(journal, indent=2)}\n",
                encoding="utf-8",
            )
            workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
            original = workflow.read_bytes()
            environment = os.environ.copy()
            environment["HERDR_ORCHESTRATOR_TEST_PRESERVED_DISCOVERY_BARRIER"] = str(barrier)
            process = subprocess.Popen(
                [
                    *self._node_command(environment),
                    "uninstall",
                    "--project",
                    str(project),
                ],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            try:
                deadline = time.monotonic() + 5
                while not list(barrier.glob("*.ready")) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(list(barrier.glob("*.ready")))
                self.assertIsNone(process.poll())
                edited = original + b"concurrent user edit\n"
                workflow.write_bytes(edited)
            finally:
                barrier.mkdir(parents=True, exist_ok=True)
                (barrier / "release").write_text("", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=30)

            self.assertEqual(process.returncode, 2, f"{stderr}\n{stdout}")
            self.assertTrue(stderr.startswith("installer_recovery_conflict: "))
            self.assertIn(".herdr-orchestrator/workflows/multi-harness.toml", stderr)
            self.assertEqual(workflow.read_bytes(), edited)
            self.assertTrue(journal_path.is_file())
            self.assertTrue((project / ".herdr-orchestrator/manifest.json").is_file())

    def test_git_exclude_preserves_mode_under_a_restrictive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(project)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            exclude = project / ".git/info/exclude"
            exclude.write_bytes(b"# caller-owned exclude\n")
            exclude.chmod(0o664)

            install = subprocess.run(
                [
                    *self._node_command(None),
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "droid",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                preexec_fn=lambda: os.umask(0o077),
                timeout=30,
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertEqual(exclude.stat().st_mode & 0o777, 0o664)

    def test_recovery_preserves_a_live_file_when_only_its_mode_changed(self) -> None:
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
            workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
            original = workflow.read_bytes()
            published_environment = os.environ.copy()
            published_environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL"] = (
                "journal:published"
            )
            published = self._run(
                "upgrade",
                "--project",
                str(project),
                "--harness",
                "droid",
                "--harness",
                "codex",
                env=published_environment,
            )
            self.assertEqual(published.returncode, 86, published.stderr)
            journal_path = project / ".herdr-orchestrator/install-journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            operation = next(
                item
                for item in journal["operations"]
                if item["target"].get("path") == ".herdr-orchestrator/workflows/multi-harness.toml"
            )
            temporary_environment = os.environ.copy()
            temporary_environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX"] = (
                f"temporary:target:{operation['id']}:"
            )
            interrupted = self._run(
                "upgrade",
                "--project",
                str(project),
                "--harness",
                "droid",
                "--harness",
                "codex",
                env=temporary_environment,
            )
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)

            workflow.chmod(0o600)
            doctor_environment = os.environ.copy()
            doctor_environment["PYTHON"] = "/bin/false"
            doctor = self._run(
                "doctor",
                "--project",
                str(project),
                env=doctor_environment,
            )
            installation = json.loads(doctor.stdout)["installation"]
            self.assertIn(
                ".herdr-orchestrator/workflows/multi-harness.toml",
                installation["journal"]["conflicts"],
            )

            recovered = self._run(
                "upgrade",
                "--project",
                str(project),
                "--harness",
                "droid",
                "--harness",
                "codex",
            )

            self.assertEqual(recovered.returncode, 2)
            self.assertEqual(
                recovered.stderr.strip(),
                "installer_recovery_conflict: " ".herdr-orchestrator/workflows/multi-harness.toml",
            )
            self.assertEqual(workflow.read_bytes(), original)
            self.assertEqual(workflow.stat().st_mode & 0o777, 0o600)
            self.assertTrue(journal_path.is_file())

    def test_live_transaction_owner_blocks_recovery_after_a_target_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            barrier = root / "barrier"
            (project / ".git").mkdir(parents=True)
            owner_environment = os.environ.copy()
            owner_environment.update(
                {
                    "HERDR_ORCHESTRATOR_TEST_PAUSE_AT_LABEL_PREFIX": ("target:operation-1:"),
                    "HERDR_ORCHESTRATOR_TEST_PAUSE_BARRIER": str(barrier),
                }
            )
            owner = subprocess.Popen(
                [
                    *self._node_command(owner_environment),
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "droid",
                ],
                cwd=REPO_ROOT,
                env=owner_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            ready = False
            owner_was_running = False
            before: dict[str, tuple[bytes, int]] = {}
            after: dict[str, tuple[bytes, int]] = {}
            contender: subprocess.CompletedProcess[str] | None = None

            def project_files() -> dict[str, tuple[bytes, int]]:
                return {
                    path.relative_to(project).as_posix(): (
                        path.read_bytes(),
                        path.stat().st_mode & 0o7777,
                    )
                    for path in sorted(project.rglob("*"))
                    if path.is_file()
                }

            try:
                deadline = time.monotonic() + 5
                while not list(barrier.glob("*.ready")) and time.monotonic() < deadline:
                    time.sleep(0.01)
                ready = bool(list(barrier.glob("*.ready")))
                owner_was_running = owner.poll() is None
                before = project_files()
                contender = self._run(
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "codex",
                )
                after = project_files()
            finally:
                barrier.mkdir(parents=True, exist_ok=True)
                (barrier / "release").write_text("", encoding="utf-8")
                owner_stdout, owner_stderr = owner.communicate(timeout=30)

            self.assertTrue(ready)
            self.assertTrue(owner_was_running)
            self.assertIsNotNone(contender)
            assert contender is not None
            self.assertEqual(contender.returncode, 2, contender.stderr)
            self.assertEqual(contender.stderr.strip(), "installer_transaction_active")
            self.assertEqual(after, before)
            self.assertEqual(owner.returncode, 0, (owner_stdout, owner_stderr))
            self.assertTrue((project / ".herdr-orchestrator/manifest.json").is_file())
            self.assertFalse((project / ".herdr-orchestrator/install-journal.json").exists())

    def _run(
        self,
        *arguments: str,
        env: dict[str, str] | None = None,
        preexec_fn=None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._node_command(env), *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            preexec_fn=preexec_fn,
            timeout=30,
        )

    def _node_command(self, env: dict[str, str] | None) -> list[str]:
        command = ["node"]
        if env is not None and any(name.startswith("HERDR_ORCHESTRATOR_TEST_") for name in env):
            command.extend(["--no-warnings", "--loader", str(INSTALLER_FAULT_LOADER)])
        command.append(str(CLI))
        return command


if __name__ == "__main__":
    unittest.main()
