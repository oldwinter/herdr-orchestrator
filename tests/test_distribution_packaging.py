from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator import __version__
from tests.test_distribution import (
    MANAGER_PACKAGE,
    REPO_ROOT,
    DistributionCliMixin,
)


class DistributionCliPackagingTests(DistributionCliMixin, unittest.TestCase):
    def test_just_manager_defaults_to_grok(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            probe = root / "manager-probe"
            harness = commands / "grok"
            harness.write_text(
                "#!/bin/sh\n" 'pwd > "$MANAGER_PROBE"\n',
                encoding="utf-8",
            )
            harness.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HERDR_ENV": "1",
                    "MANAGER_PROBE": str(probe),
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                }
            )

            manager = subprocess.run(
                ["just", "manager"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            self.assertEqual(manager.returncode, 0, manager.stderr)
            self.assertEqual(
                probe.read_text(encoding="utf-8").strip(),
                str(REPO_ROOT / "manager"),
            )

    def test_just_install_manager_installs_the_cli_then_manager_light(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            probe = root / "npm-probe"
            manager_light_probe = root / "manager-light-probe"
            npm = commands / "npm"
            npm.write_text(
                "#!/bin/sh\n" 'printf "%s\\n" "$@" > "$NPM_PROBE"\n',
                encoding="utf-8",
            )
            npm.chmod(0o755)
            manager_light = commands / "herdr-orchestrator"
            manager_light.write_text(
                "#!/bin/sh\n" 'printf "%s\\n" "$@" > "$MANAGER_LIGHT_PROBE"\n',
                encoding="utf-8",
            )
            manager_light.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "NPM_PROBE": str(probe),
                    "MANAGER_LIGHT_PROBE": str(manager_light_probe),
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                }
            )

            install = subprocess.run(
                ["just", "install-manager"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertEqual(
                probe.read_text(encoding="utf-8").splitlines(),
                ["install", "--global", "."],
            )
            self.assertEqual(
                manager_light_probe.read_text(encoding="utf-8").splitlines(),
                ["manager-light", "install"],
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

    def test_install_keeps_a_real_git_worktree_status_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(project)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            (project / "README.md").write_text("# Existing project\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(project), "add", "README.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            before = self._git_status(project)

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertEqual(self._git_status(project), before)
            exclude = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "--git-path", "info/exclude"],
                check=True,
                capture_output=True,
                text=True,
            )
            exclude_path = Path(exclude.stdout.strip())
            if not exclude_path.is_absolute():
                exclude_path = project / exclude_path
            exclude_text = exclude_path.read_text(encoding="utf-8")
            self.assertIn("/.herdr-orchestrator/", exclude_text)
            self.assertIn("/.orchestrator/", exclude_text)
            self.assertIn("/.agents/skills/herdr-orchestrator/", exclude_text)

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

    def test_install_allows_a_linked_worktree_common_git_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            checkout = root / "linked-checkout"
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            (repository / "README.md").write_text("# Existing project\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repository), "add", "README.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "--quiet",
                    "-m",
                    "initial",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "worktree",
                    "add",
                    "--quiet",
                    "-b",
                    "linked",
                    str(checkout),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            before = self._git_status(checkout)

            install = self._run(
                "install",
                "--project",
                str(checkout),
                "--harness",
                "droid",
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertEqual(self._git_status(checkout), before)
            exclude = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "--git-path", "info/exclude"],
                check=True,
                capture_output=True,
                text=True,
            )
            exclude_path = Path(exclude.stdout.strip())
            self.assertTrue(exclude_path.is_absolute())
            exclude_text = exclude_path.read_text(encoding="utf-8")
            self.assertIn("/.herdr-orchestrator/", exclude_text)

    def test_existing_skill_router_requires_explicit_project_skill_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            existing = project / ".agents/skills/existing/SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("---\nname: existing\n---\n", encoding="utf-8")

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            payload = json.loads(install.stdout)
            self.assertEqual(payload["skill"], "skipped_existing_router")
            skill = project / ".agents/skills/herdr-orchestrator/SKILL.md"
            self.assertFalse(skill.exists())

            explicit = self._run(
                "upgrade",
                "--project",
                str(project),
                "--install-skill",
            )

            self.assertEqual(explicit.returncode, 0, explicit.stderr)
            self.assertEqual(json.loads(explicit.stdout)["skill"], "managed")
            self.assertTrue(skill.is_file())

    def test_install_rejects_a_symlinked_git_exclude_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            sentinel = root / "outside-exclude"
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(project)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            exclude = project / ".git/info/exclude"
            exclude.unlink()
            sentinel.write_text("outside\n", encoding="utf-8")
            os.symlink(sentinel, exclude)

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(install.returncode, 2)
            self.assertIn("git_exclude_symlink", install.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")
            self.assertFalse((project / ".herdr-orchestrator").exists())

    def test_install_rejects_a_non_regular_git_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(project)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            exclude = project / ".git/info/exclude"
            exclude.unlink()
            exclude.mkdir()

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(install.returncode, 2)
            self.assertIn("git_exclude_not_regular", install.stderr)
            self.assertFalse((project / ".herdr-orchestrator").exists())

    def test_install_rejects_a_symlinked_git_directory_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            project = root / "project"
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            project.mkdir()
            os.symlink(outside / ".git", project / ".git", target_is_directory=True)
            exclude = outside / ".git/info/exclude"
            before = exclude.read_text(encoding="utf-8")

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(install.returncode, 2)
            self.assertIn("git_exclude_symlink", install.stderr)
            self.assertEqual(exclude.read_text(encoding="utf-8"), before)
            self.assertFalse((project / ".herdr-orchestrator").exists())

    def test_reinstall_preserves_user_modified_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            first = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
            custom = f"{workflow.read_text(encoding='utf-8')}# user setting\n"
            workflow.write_text(custom, encoding="utf-8")

            second = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(second.returncode, 1, second.stderr)
            payload = json.loads(second.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["preserved"],
                [".herdr-orchestrator/workflows/multi-harness.toml"],
            )
            self.assertEqual(workflow.read_text(encoding="utf-8"), custom)

            doctor = self._run("doctor", "--project", str(project))

            self.assertEqual(doctor.returncode, 1, doctor.stderr)
            doctor_payload = json.loads(doctor.stdout)
            self.assertFalse(doctor_payload["ok"])
            self.assertEqual(
                doctor_payload["installation"]["modified"],
                [".herdr-orchestrator/workflows/multi-harness.toml"],
            )

    def test_uninstall_removes_only_unchanged_managed_files(self) -> None:
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
            skill = project / ".agents/skills/herdr-orchestrator/SKILL.md"
            custom = f"{skill.read_text(encoding='utf-8')}\nUser note.\n"
            skill.write_text(custom, encoding="utf-8")

            uninstall = self._run("uninstall", "--project", str(project))

            self.assertEqual(uninstall.returncode, 1, uninstall.stderr)
            payload = json.loads(uninstall.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["preserved"],
                [".agents/skills/herdr-orchestrator/SKILL.md"],
            )
            self.assertEqual(skill.read_text(encoding="utf-8"), custom)
            self.assertFalse(
                (project / ".herdr-orchestrator/workflows/multi-harness.toml").exists()
            )
            self.assertFalse((project / ".herdr-orchestrator/manifest.json").exists())

    def test_uninstall_preflights_symlinks_before_deleting_managed_files(self) -> None:
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
            workflow = project / ".herdr-orchestrator/workflows/multi-harness.toml"
            manifest = project / ".herdr-orchestrator/manifest.json"
            outside = root / "outside"
            outside.write_text("outside", encoding="utf-8")
            profile = project / ".herdr-orchestrator/profiles/harnesses/droid.toml"
            profile.unlink()
            profile.symlink_to(outside)

            uninstall = self._run("uninstall", "--project", str(project))

            self.assertEqual(uninstall.returncode, 2)
            self.assertIn("managed_path_symlink", uninstall.stderr)
            self.assertTrue(workflow.is_file())
            self.assertTrue(manifest.is_file())
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_install_rejects_symlinked_managed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            (project / ".git").mkdir()
            os.symlink(outside, project / ".orchestrator", target_is_directory=True)

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(install.returncode, 2)
            self.assertIn("managed_path_symlink", install.stderr)
            self.assertFalse((outside / ".gitignore").exists())

    def test_install_rejects_a_non_regular_managed_target_before_journaling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            target = project / ".orchestrator/.gitignore"
            target.mkdir(parents=True)

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

            self.assertEqual(install.returncode, 2)
            self.assertEqual(
                install.stderr.strip(),
                "managed_path_not_regular: .orchestrator/.gitignore",
            )
            self.assertFalse((project / ".herdr-orchestrator/install-journal.json").exists())
            self.assertFalse((project / ".herdr-orchestrator/manifest.json").exists())

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

    def test_upgrade_reconciles_the_selected_harness_catalog(self) -> None:
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

            unchanged = self._run("upgrade", "--project", str(project))

            self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
            self.assertEqual(json.loads(unchanged.stdout)["harnesses"], ["droid"])

            upgrade = self._run(
                "upgrade",
                "--project",
                str(project),
                "--harness",
                "droid",
                "--harness",
                "codex",
            )

            self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
            payload = json.loads(upgrade.stdout)
            self.assertEqual(payload["harnesses"], ["droid", "codex"])
            catalog = self._run("catalog", "--project", str(project))
            self.assertEqual(catalog.returncode, 0, catalog.stderr)
            self.assertEqual(
                [item["harness"] for item in json.loads(catalog.stdout)["harnesses"]],
                ["droid", "codex"],
            )

    def test_packed_npm_cli_runs_outside_the_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_directory = root / "package"
            project = root / "project"
            package_directory.mkdir()
            project.mkdir()
            (project / ".git").mkdir()
            packed = subprocess.run(
                [
                    "npm",
                    "pack",
                    "--silent",
                    "--pack-destination",
                    str(package_directory),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(packed.returncode, 0, packed.stderr)
            tarball = package_directory / packed.stdout.strip().splitlines()[-1]
            with tarfile.open(tarball) as archive:
                packaged_files = set(archive.getnames())
            self.assertIn(
                "package/src/herdr_orchestrator/dashboard/static/cytoscape.min.js",
                packaged_files,
            )
            self.assertIn(
                "package/src/herdr_orchestrator/dashboard/static/topology.js",
                packaged_files,
            )
            self.assertIn(
                "package/src/herdr_orchestrator/dashboard/static/cytoscape.LICENSE.txt",
                packaged_files,
            )
            self.assertIn("package/manager/AGENTS.md", packaged_files)
            self.assertIn("package/plugins/manager-light/configure.mjs", packaged_files)
            self.assertIn("package/plugins/manager-light/herdr-plugin.toml", packaged_files)
            self.assertIn("package/plugins/manager-light/hook.mjs", packaged_files)
            self.assertIn("package/plugins/manager-light/projection.mjs", packaged_files)

            commands = root / "bin"
            commands.mkdir()
            manager_probe = root / "manager-probe"
            harness = commands / "grok"
            harness.write_text(
                "#!/bin/sh\n" 'pwd > "$MANAGER_PROBE"\n',
                encoding="utf-8",
            )
            harness.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HERDR_ENV": "1",
                    "MANAGER_PROBE": str(manager_probe),
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                }
            )
            manager = subprocess.run(
                [
                    "npm",
                    "exec",
                    "--yes",
                    "--package",
                    str(tarball),
                    "--",
                    "herdr-manager",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=60,
            )
            self.assertEqual(manager.returncode, 0, manager.stderr)
            manager_directory = Path(manager_probe.read_text(encoding="utf-8").strip())
            self.assertEqual(manager_directory.name, "manager")
            self.assertTrue((manager_directory / "AGENTS.md").is_file())

            install = subprocess.run(
                [
                    "npm",
                    "exec",
                    "--yes",
                    "--package",
                    str(tarball),
                    "--",
                    "herdr-orchestrator",
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "droid",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertTrue(
                (project / ".herdr-orchestrator/profiles/harnesses/droid.toml").is_file()
            )
            self.assertTrue((project / ".herdr-orchestrator/manager/AGENTS.md").is_file())
            self.assertTrue((project / ".agents/skills/herdr-orchestrator/SKILL.md").is_file())

    def test_packed_herdr_manager_package_runs_outside_the_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_directory = root / "packages"
            install_directory = root / "install"
            commands = root / "bin"
            package_directory.mkdir()
            install_directory.mkdir()
            commands.mkdir()
            tarballs: list[Path] = []
            for source in (REPO_ROOT, MANAGER_PACKAGE):
                packed = subprocess.run(
                    [
                        "npm",
                        "pack",
                        "--silent",
                        "--pack-destination",
                        str(package_directory),
                    ],
                    cwd=source,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(packed.returncode, 0, packed.stderr)
                tarballs.append(package_directory / packed.stdout.strip().splitlines()[-1])
            installed = subprocess.run(
                [
                    "npm",
                    "install",
                    "--offline",
                    "--ignore-scripts",
                    "--no-package-lock",
                    *map(str, tarballs),
                ],
                cwd=install_directory,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            manager_bin = install_directory / "node_modules/.bin/herdr-manager"
            self.assertTrue(
                manager_bin.samefile(
                    install_directory / "node_modules/herdr-manager/bin/herdr-manager.mjs"
                )
            )
            probe = root / "manager-probe"
            for name in ("grok", "codex", "claude"):
                harness = commands / name
                if name == "codex":
                    harness.write_text(
                        "#!/bin/sh\n"
                        'if [ "${1:-}" = "--version" ]; then exit 0; fi\n'
                        'pwd > "$MANAGER_PROBE"\n',
                        encoding="utf-8",
                    )
                else:
                    harness.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                harness.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HERDR_BIN_PATH": "/bin/true",
                    "HERDR_ENV": "1",
                    "MANAGER_PROBE": str(probe),
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                }
            )

            manager = subprocess.run(
                [str(manager_bin)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            self.assertEqual(manager.returncode, 0, manager.stderr)
            manager_directory = Path(probe.read_text(encoding="utf-8").strip())
            self.assertEqual(manager_directory.name, "manager")
            self.assertTrue((manager_directory / "AGENTS.md").is_file())

    def test_uninstall_rejects_manifest_paths_outside_managed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            valuable = root / "valuable.txt"
            valuable.write_text("keep", encoding="utf-8")
            manifest_directory = project / ".herdr-orchestrator"
            manifest_directory.mkdir()
            manifest = {
                "schema_version": 1,
                "package": "herdr-orchestrator",
                "version": "0.1.0",
                "harnesses": ["droid"],
                "files": {
                    "../valuable.txt": hashlib.sha256(b"keep").hexdigest(),
                },
            }
            (manifest_directory / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            uninstall = self._run("uninstall", "--project", str(project))

            self.assertEqual(uninstall.returncode, 2)
            self.assertIn("manifest_entry_invalid", uninstall.stderr)
            self.assertEqual(valuable.read_text(encoding="utf-8"), "keep")

    def test_install_does_not_take_ownership_of_an_existing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(project)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            skill = project / ".agents/skills/herdr-orchestrator/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                (REPO_ROOT / "skills/herdr-orchestrator/SKILL.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                "--install-skill",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            payload = json.loads(install.stdout)
            self.assertEqual(
                payload["unmanaged"],
                [".agents/skills/herdr-orchestrator/SKILL.md"],
            )
            manifest = json.loads(
                (project / ".herdr-orchestrator/manifest.json").read_text(encoding="utf-8")
            )
            self.assertNotIn(
                ".agents/skills/herdr-orchestrator/SKILL.md",
                manifest["files"],
            )
            exclude = project / ".git/info/exclude"
            if exclude.exists():
                self.assertNotIn(
                    "/.agents/skills/herdr-orchestrator/",
                    exclude.read_text(encoding="utf-8"),
                )

            uninstall = self._run("uninstall", "--project", str(project))

            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertTrue(skill.is_file())
            self.assertNotIn(
                "# BEGIN herdr-orchestrator managed paths",
                exclude.read_text(encoding="utf-8"),
            )



if __name__ == "__main__":
    unittest.main()
