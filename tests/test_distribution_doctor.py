from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator import __version__
from test_distribution import DistributionCliFixture


class DistributionDoctorTests(DistributionCliFixture, unittest.TestCase):
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

    def test_wrapper_forwards_doctor_probe_timeout(self) -> None:
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

            doctor = self._run(
                "doctor",
                "--project",
                str(project),
                "--probe-timeout-seconds",
                "1",
            )

        self.assertEqual(doctor.returncode, 1, doctor.stderr)
        payload = json.loads(doctor.stdout)
        self.assertEqual(
            payload["runtime"]["error"],
            "doctor_probe_timeout_out_of_range",
        )

    def test_doctor_rejects_a_nonzero_runtime_exit_with_a_healthy_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / ".git").mkdir()
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            fake_python = root / "python"
            fake_python.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' \'{"checks": [], "ok": true, "summary": {}}\'\n'
                "exit 9\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = os.environ.copy()
            environment["PYTHON"] = str(fake_python)

            doctor = self._run(
                "doctor",
                "--project",
                str(project),
                env=environment,
            )

        self.assertEqual(doctor.returncode, 1, doctor.stderr)
        payload = json.loads(doctor.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["runtime"]["ok"])
        self.assertEqual(payload["runtime"]["error"], "runtime_doctor_exit: 9")

    def test_doctor_rejects_a_runtime_payload_with_a_non_boolean_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / ".git").mkdir()
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            fake_python = root / "python"
            fake_python.write_text(
                "#!/bin/sh\n" 'printf \'%s\\n\' \'{"checks": [], "ok": "yes", "summary": {}}\'\n',
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = os.environ.copy()
            environment["PYTHON"] = str(fake_python)

            doctor = self._run(
                "doctor",
                "--project",
                str(project),
                env=environment,
            )

        self.assertEqual(doctor.returncode, 1, doctor.stderr)
        payload = json.loads(doctor.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["runtime"]["error"], "runtime_doctor_invalid_output")

    def test_doctor_bounds_an_interpreter_startup_hang(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / ".git").mkdir()
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            fake_python = root / "python"
            fake_python.write_text(
                "#!/bin/sh\nsleep 2\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHON": str(fake_python),
                    "HERDR_ORCHESTRATOR_DOCTOR_TIMEOUT_MS": "100",
                }
            )

            doctor = self._run(
                "doctor",
                "--project",
                str(project),
                env=environment,
            )

        self.assertEqual(doctor.returncode, 1, doctor.stderr)
        payload = json.loads(doctor.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["runtime"], {"error": "runtime_doctor_timeout", "ok": False})

    def test_doctor_reports_wrapper_manifest_version_skew(self) -> None:
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
            manifest_path = project / ".herdr-orchestrator/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = "0.0.0"
            manifest_path.write_text(
                f"{json.dumps(manifest, indent=2)}\n",
                encoding="utf-8",
            )

            doctor = self._run("doctor", "--project", str(project))

        self.assertEqual(doctor.returncode, 1, doctor.stderr)
        payload = json.loads(doctor.stdout)
        installation = payload["installation"]
        self.assertFalse(installation["ok"])
        self.assertTrue(installation["version_skew"])
        self.assertEqual(installation["installed_version"], "0.0.0")
        self.assertEqual(installation["runtime_version"], __version__)

    def test_doctor_compares_package_manifest_bytes_and_git_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            commands = root / "bin"
            commands.mkdir()
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(project)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            fake_python = commands / "python"
            fake_python.write_text(
                "#!/bin/sh\nprintf '%s\\n' '{\"ok\":false,\"checks\":[]}'\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = os.environ.copy()
            environment["PYTHON"] = str(fake_python)

            healthy = self._run(
                "doctor",
                "--project",
                str(project),
                env=environment,
            )

            self.assertEqual(healthy.returncode, 1, healthy.stderr)
            healthy_installation = json.loads(healthy.stdout)["installation"]
            self.assertTrue(healthy_installation["ok"])
            self.assertEqual(healthy_installation["package_missing"], [])
            self.assertEqual(healthy_installation["package_modified"], [])
            self.assertEqual(healthy_installation["manifest_missing"], [])
            self.assertEqual(healthy_installation["manifest_extra"], [])
            self.assertEqual(healthy_installation["manifest_mismatched"], [])
            self.assertTrue(healthy_installation["git_exclude"]["ok"])

            manifest_path = project / ".herdr-orchestrator/manifest.json"
            original_manifest = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(original_manifest)
            workflow_path = ".herdr-orchestrator/workflows/multi-harness.toml"
            del manifest["files"][workflow_path]
            del manifest["file_modes"][workflow_path]
            manifest_path.write_text(
                f"{json.dumps(manifest, indent=2)}\n",
                encoding="utf-8",
            )
            missing_claim = self._run(
                "doctor",
                "--project",
                str(project),
                env=environment,
            )

            self.assertEqual(missing_claim.returncode, 1, missing_claim.stderr)
            missing_installation = json.loads(missing_claim.stdout)["installation"]
            self.assertFalse(missing_installation["ok"])
            self.assertEqual(missing_installation["manifest_missing"], [workflow_path])
            self.assertEqual(missing_installation["package_missing"], [])
            manifest_path.write_text(original_manifest, encoding="utf-8")

            exclude = project / ".git/info/exclude"
            exclude.write_text(
                exclude.read_text(encoding="utf-8").replace(
                    "/.orchestrator/",
                    "/.orchestrator-tampered/",
                ),
                encoding="utf-8",
            )
            damaged_exclude = self._run(
                "doctor",
                "--project",
                str(project),
                env=environment,
            )

            self.assertEqual(damaged_exclude.returncode, 1, damaged_exclude.stderr)
            damaged_installation = json.loads(damaged_exclude.stdout)["installation"]
            self.assertFalse(damaged_installation["ok"])
            self.assertFalse(damaged_installation["git_exclude"]["ok"])
            self.assertEqual(
                damaged_installation["git_exclude"]["status"],
                "modified",
            )


if __name__ == "__main__":
    unittest.main()
