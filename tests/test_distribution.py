from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path

from herdr_orchestrator import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin/herdr-orchestrator.mjs"
MANAGER_PACKAGE = REPO_ROOT / "packages/herdr-manager"


class DistributionCliTests(unittest.TestCase):
    def test_install_help_has_no_filesystem_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            before = sorted(path.relative_to(project) for path in project.rglob("*"))

            result = self._run(
                "install",
                "--project",
                str(project),
                "--help",
            )
            after = sorted(path.relative_to(project) for path in project.rglob("*"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: herdr-orchestrator install", result.stdout)
        self.assertEqual(after, before)

    def test_version_matches_the_python_distribution(self) -> None:
        version = self._run("--version")

        self.assertEqual(version.returncode, 0, version.stderr)
        python_version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
        self.assertEqual(version.stdout.strip(), python_version)
        self.assertEqual(__version__, python_version)

    def test_manager_package_homepage_targets_its_own_readme(self) -> None:
        metadata = json.loads((MANAGER_PACKAGE / "package.json").read_text(encoding="utf-8"))
        repository = metadata["repository"]
        repository_url = repository["url"].removeprefix("git+").removesuffix(".git")

        self.assertEqual(
            metadata["homepage"],
            f"{repository_url}/tree/main/{repository['directory']}#readme",
        )

    def test_npm_test_script_exposes_the_src_layout(self) -> None:
        metadata = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))

        self.assertIn("PYTHONPATH=src", metadata["scripts"]["test"])

    def test_manager_runtime_dependency_is_exact_and_locked(self) -> None:
        metadata = json.loads((MANAGER_PACKAGE / "package.json").read_text(encoding="utf-8"))
        dependency = metadata["dependencies"]["herdr-orchestrator"]
        runtime = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))

        self.assertRegex(dependency, r"^\d+\.\d+\.\d+$")
        self.assertEqual(dependency, runtime["version"])
        lock_path = MANAGER_PACKAGE / "package-lock.json"
        self.assertTrue(lock_path.is_file())
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        root = lock["packages"][""]
        resolved = lock["packages"]["node_modules/herdr-orchestrator"]
        self.assertEqual(root["dependencies"]["herdr-orchestrator"], dependency)
        self.assertEqual(resolved["version"], dependency)
        self.assertRegex(resolved["resolved"], r"^https://")
        self.assertRegex(resolved["integrity"], r"^sha512-")

    def test_npm_dependency_audit_covers_both_package_lockfiles(self) -> None:
        security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")

        self.assertIn("npm audit --package-lock-only", security)
        self.assertIn("packages/herdr-manager", security)
        self.assertIn("npm audit --package-lock-only", justfile)
        self.assertIn("npm audit --package-lock-only --prefix packages/herdr-manager", justfile)

    def test_missing_option_value_returns_a_stable_cli_error(self) -> None:
        result = self._run("install", "--project")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "option_value_required: --project")

    def test_runtime_rejects_a_workflow_override(self) -> None:
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

            for override in (
                ["--workflow", str(project / "other.toml")],
                [f"--workflow={project / 'other.toml'}"],
                *[
                    [f"--{prefix}={project / 'other.toml'}"]
                    for prefix in ("w", "wo", "wor", "work", "workf", "workfl", "workflo")
                ],
            ):
                with self.subTest(override=override):
                    result = self._run(
                        "catalog",
                        "--project",
                        str(project),
                        *override,
                    )

                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stderr.strip(), "workflow_option_reserved")

    def test_setup_rejects_unknown_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()

            result = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                "--unexpected",
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "option_unsupported: --unexpected")

    def test_install_does_not_follow_an_environment_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            outside = root / "outside.git"
            initialized = subprocess.run(
                ["git", "init", "--bare", "--quiet", str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            exclude = outside / "info/exclude"
            before = exclude.read_text(encoding="utf-8")
            environment = os.environ.copy()
            environment["GIT_DIR"] = str(outside)

            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
                env=environment,
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertEqual(json.loads(install.stdout)["local_exclude"], "unavailable")
            self.assertEqual(exclude.read_text(encoding="utf-8"), before)

    def test_malformed_manifest_returns_a_stable_error(self) -> None:
        for content in ("null", "{"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                (project / ".git").mkdir()
                manifest = project / ".herdr-orchestrator/manifest.json"
                manifest.parent.mkdir()
                manifest.write_text(content, encoding="utf-8")

                result = self._run(
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "droid",
                )

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr.strip(), "manifest_invalid")

    def test_manifest_rejects_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            manifest = project / ".herdr-orchestrator/manifest.json"
            manifest.parent.mkdir()
            manifest.write_bytes(
                b'{"schema_version":1,"package":"herdr-orchestrator",'
                b'"version":"0.1.6\xff","harnesses":["droid"],"files":{}}'
            )

            result = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "droid",
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "manifest_invalid")

    def test_install_bootstraps_a_portable_project_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()

            install = self._run("install", "--project", str(project), "--harness", "droid")

            self.assertEqual(install.returncode, 0, install.stderr)
            payload = json.loads(install.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["harnesses"], ["droid"])
            self.assertEqual(payload["manager"], ".herdr-orchestrator/manager")
            self.assertTrue(
                (project / ".herdr-orchestrator/workflows/multi-harness.toml").is_file()
            )
            self.assertTrue((project / ".herdr-orchestrator/manager/AGENTS.md").is_file())
            self.assertTrue((project / ".herdr-orchestrator/manager/CLAUDE.md").is_file())
            self.assertTrue((project / ".agents/skills/herdr-orchestrator/SKILL.md").is_file())

            catalog = self._run("catalog", "--project", str(project))

            self.assertEqual(catalog.returncode, 0, catalog.stderr)
            catalog_payload = json.loads(catalog.stdout)
            self.assertEqual(
                [item["harness"] for item in catalog_payload["harnesses"]],
                ["droid"],
            )

    def test_install_recovers_after_interruption_at_journal_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            interrupted_environment = os.environ.copy()
            interrupted_environment.update(
                {
                    "NODE_ENV": "test",
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION": "2",
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
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION": "2",
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
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION": "2",
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
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION": "2",
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

            doctor = self._run("doctor", "--project", str(project))

            self.assertEqual(doctor.returncode, 1, doctor.stderr)
            installation = json.loads(doctor.stdout)["installation"]
            self.assertFalse(installation["ok"])
            self.assertFalse(installation["manifest"])
            self.assertTrue(installation["journal"]["active"])
            self.assertEqual(installation["journal"]["command"], "install")
            self.assertEqual(installation["journal"]["conflicts"], [])
            self.assertEqual(journal_path.read_bytes(), before)
            self.assertFalse(
                (project / ".herdr-orchestrator/workflows/multi-harness.toml").exists()
            )

    def test_recovery_conflict_preserves_the_journal_and_user_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            interrupted_environment = os.environ.copy()
            interrupted_environment.update(
                {
                    "NODE_ENV": "test",
                    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION": "2",
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

    def test_manager_requires_a_herdr_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "claude",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            outside_herdr = os.environ.copy()
            outside_herdr.pop("HERDR_ENV", None)

            manager = self._run(
                "manager",
                "--project",
                str(project),
                "--harness",
                "claude",
                env=outside_herdr,
            )

        self.assertEqual(manager.returncode, 2)
        self.assertEqual(manager.stderr.strip(), "manager_requires_herdr: HERDR_ENV=1")

    def test_manager_rejects_a_harness_not_enabled_by_the_installation(self) -> None:
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
            environment = os.environ.copy()
            environment["HERDR_ENV"] = "1"

            manager = self._run(
                "manager",
                "--project",
                str(project),
                "--harness",
                "claude",
                env=environment,
            )

        self.assertEqual(manager.returncode, 2)
        self.assertEqual(
            manager.stderr.strip(),
            "manager_harness_not_enabled: claude",
        )

    def test_manager_default_respects_project_enabled_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            commands = root / "bin"
            project.mkdir()
            commands.mkdir()
            (project / ".git").mkdir()
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "codex",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            probe = root / "manager-probe"
            for name in ("grok", "codex"):
                harness = commands / name
                harness.write_text(
                    "#!/bin/sh\n"
                    'if [ "${1:-}" = "--version" ]; then exit 0; fi\n'
                    f'printf "{name}\\n" > "$MANAGER_PROBE"\n',
                    encoding="utf-8",
                )
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

            manager = self._run(
                "manager",
                "--project",
                str(project),
                env=environment,
            )

            self.assertEqual(manager.returncode, 0, manager.stderr)
            self.assertEqual(probe.read_text(encoding="utf-8").strip(), "codex")

    def test_manager_launches_in_the_installed_workspace_without_extra_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            commands = root / "bin"
            project.mkdir()
            commands.mkdir()
            (project / ".git").mkdir()
            install = self._run(
                "install",
                "--project",
                str(project),
                "--harness",
                "claude",
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            probe = root / "manager-probe"
            harness = commands / "claude"
            harness.write_text(
                "#!/bin/sh\n"
                'pwd > "$MANAGER_PROBE"\n'
                'if [ "$#" -gt 0 ]; then\n'
                '  printf \'%s\\n\' "$@" >> "$MANAGER_PROBE"\n'
                "fi\n",
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

            manager = self._run(
                "manager",
                "--project",
                str(project),
                "--harness",
                "claude",
                env=environment,
            )

            self.assertEqual(manager.returncode, 0, manager.stderr)
            probe_lines = probe.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(probe_lines), 1)
            self.assertTrue(Path(probe_lines[0]).samefile(project / ".herdr-orchestrator/manager"))

    def test_manager_reports_and_clears_a_complete_best_effort_token_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            harness = commands / "grok"
            harness.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            harness.chmod(0o755)
            metadata_probe = root / "metadata.jsonl"
            herdr = commands / "herdr"
            herdr.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['METADATA_PROBE'], 'a', encoding='utf-8') as probe:\n"
                "    probe.write(json.dumps(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            herdr.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HERDR_BIN_PATH": str(herdr),
                    "HERDR_ENV": "1",
                    "HERDR_PANE_ID": "manager-pane",
                    "METADATA_PROBE": str(metadata_probe),
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                }
            )

            manager = self._run("manager", "grok", env=environment)

            self.assertEqual(manager.returncode, 0, manager.stderr)
            calls = [
                json.loads(line) for line in metadata_probe.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][:3], ["pane", "report-metadata", "manager-pane"])
            self.assertIn("hml_role=manager", calls[0])
            self.assertIn("hml_manager=●", calls[0])
            self.assertEqual(calls[0].count("--token"), 2)
            self.assertEqual(calls[0].count("--clear-token"), 4)
            self.assertEqual(calls[1].count("--token"), 0)
            self.assertEqual(calls[1].count("--clear-token"), 6)

    def test_manager_accepts_a_positional_harness_from_any_directory(self) -> None:
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

            manager = self._run(
                "manager",
                "grok",
                env=environment,
                cwd=root,
            )

            self.assertEqual(manager.returncode, 0, manager.stderr)
            self.assertEqual(
                probe.read_text(encoding="utf-8").strip(),
                str(REPO_ROOT / "manager"),
            )

    def test_manager_defaults_to_codex_when_grok_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            probe = root / "manager-probe"
            for name in ("grok", "codex", "claude"):
                harness = commands / name
                if name == "codex":
                    harness.write_text(
                        "#!/bin/sh\n"
                        'if [ "${1:-}" = "--version" ]; then exit 0; fi\n'
                        'printf "codex\\n" > "$MANAGER_PROBE"\n',
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

            manager = self._run("manager", env=environment, cwd=root)

            self.assertEqual(manager.returncode, 0, manager.stderr)
            self.assertEqual(probe.read_text(encoding="utf-8").strip(), "codex")

    def test_manager_default_prefers_grok_over_codex_and_claude(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            probe = root / "manager-probe"
            for name in ("grok", "codex", "claude"):
                harness = commands / name
                harness.write_text(
                    "#!/bin/sh\n"
                    'if [ "${1:-}" = "--version" ]; then exit 0; fi\n'
                    f'printf "{name}\\n" > "$MANAGER_PROBE"\n',
                    encoding="utf-8",
                )
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

            manager = self._run("manager", env=environment, cwd=root)

            self.assertEqual(manager.returncode, 0, manager.stderr)
            self.assertEqual(probe.read_text(encoding="utf-8").strip(), "grok")

    def test_manager_defaults_to_claude_when_grok_and_codex_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            probe = root / "manager-probe"
            for name in ("grok", "codex", "claude"):
                harness = commands / name
                if name == "claude":
                    harness.write_text(
                        "#!/bin/sh\n"
                        'if [ "${1:-}" = "--version" ]; then exit 0; fi\n'
                        'printf "claude\\n" > "$MANAGER_PROBE"\n',
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

            manager = self._run("manager", env=environment, cwd=root)

            self.assertEqual(manager.returncode, 0, manager.stderr)
            self.assertEqual(probe.read_text(encoding="utf-8").strip(), "claude")

    def test_manager_default_reports_when_no_supported_harness_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            for name in ("grok", "codex", "claude"):
                harness = commands / name
                harness.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                harness.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HERDR_BIN_PATH": "/bin/true",
                    "HERDR_ENV": "1",
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                }
            )

            manager = self._run("manager", env=environment, cwd=root)

            self.assertEqual(manager.returncode, 2)
            self.assertEqual(
                manager.stderr.strip(),
                "manager_default_harness_not_found: install grok, codex, or claude",
            )

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
            self.assertIn("package/manager/CLAUDE.md", packaged_files)
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
            self.assertTrue((project / ".herdr-orchestrator/manager/CLAUDE.md").is_file())
            self.assertTrue((project / ".agents/skills/herdr-orchestrator/SKILL.md").is_file())

    def test_packed_installer_recovers_after_every_durable_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_directory = root / "package"
            extracted = root / "extracted"
            package_directory.mkdir()
            extracted.mkdir()
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
                archive.extractall(extracted, filter="data")
            self.assertIn("package/bin/installer-journal.mjs", packaged_files)
            packed_cli = extracted / "package/bin/herdr-orchestrator.mjs"

            def initialize_project(project: Path) -> None:
                initialized = subprocess.run(
                    ["git", "init", "--quiet", str(project)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(initialized.returncode, 0, initialized.stderr)
                exclude = project / ".git/info/exclude"
                with exclude.open("a", encoding="utf-8") as stream:
                    stream.write("# caller-owned exclude\n/caller-cache/\n")

            def run_cli(
                project: Path,
                arguments: list[str],
                *,
                interrupt_after: int | None = None,
            ) -> subprocess.CompletedProcess[str]:
                environment = os.environ.copy()
                if interrupt_after is not None:
                    environment.update(
                        {
                            "NODE_ENV": "test",
                            "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION": str(
                                interrupt_after
                            ),
                        }
                    )
                return subprocess.run(
                    ["node", str(packed_cli), *arguments, "--project", str(project)],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                    timeout=30,
                )

            def setup_project(project: Path, operation: str) -> None:
                initialize_project(project)
                if operation in {"upgrade", "uninstall"}:
                    installed = run_cli(
                        project,
                        ["install", "--harness", "droid"],
                    )
                    self.assertEqual(installed.returncode, 0, installed.stderr)

            def operation_arguments(operation: str) -> list[str]:
                if operation == "install":
                    return ["install", "--harness", "droid"]
                if operation == "upgrade":
                    return [
                        "upgrade",
                        "--harness",
                        "droid",
                        "--harness",
                        "codex",
                    ]
                return ["uninstall"]

            def snapshot(project: Path) -> dict[str, object]:
                files: dict[str, str] = {}
                directories: list[str] = []
                for root_name in (
                    ".herdr-orchestrator",
                    ".orchestrator",
                    ".agents/skills/herdr-orchestrator",
                ):
                    managed_root = project / root_name
                    if not managed_root.exists():
                        continue
                    directories.append(root_name)
                    for path in sorted(managed_root.rglob("*")):
                        relative_path = path.relative_to(project).as_posix()
                        if path.is_dir():
                            directories.append(relative_path)
                        elif path.is_symlink():
                            files[relative_path] = f"symlink:{os.readlink(path)}"
                        else:
                            files[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()
                exclude = project / ".git/info/exclude"
                return {
                    "directories": sorted(set(directories)),
                    "exclude": exclude.read_bytes(),
                    "files": files,
                }

            mutation_counts: dict[str, int] = {}
            for operation in ("install", "upgrade", "uninstall"):
                with self.subTest(operation=operation):
                    expected_project = root / f"expected-{operation}"
                    setup_project(expected_project, operation)
                    expected_run = run_cli(
                        expected_project,
                        operation_arguments(operation),
                    )
                    self.assertEqual(expected_run.returncode, 0, expected_run.stderr)
                    expected_snapshot = snapshot(expected_project)

                    for interrupt_after in range(1, 161):
                        project = root / f"{operation}-{interrupt_after}"
                        setup_project(project, operation)
                        interrupted = run_cli(
                            project,
                            operation_arguments(operation),
                            interrupt_after=interrupt_after,
                        )
                        if interrupted.returncode != 86:
                            self.assertEqual(
                                interrupted.returncode,
                                0,
                                interrupted.stderr,
                            )
                            self.assertEqual(snapshot(project), expected_snapshot)
                            mutation_counts[operation] = interrupt_after - 1
                            break

                        recovered = run_cli(
                            project,
                            operation_arguments(operation),
                        )
                        self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        self.assertEqual(
                            snapshot(project),
                            expected_snapshot,
                            f"{operation} failed to converge after mutation " f"{interrupt_after}",
                        )
                    else:
                        self.fail(f"{operation} exceeded the mutation-matrix bound")

            self.assertGreater(mutation_counts["install"], 20)
            self.assertGreater(mutation_counts["upgrade"], 4)
            self.assertGreater(mutation_counts["uninstall"], 15)

    def test_current_package_finishes_an_older_package_transaction_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_directory = root / "packages"
            current_directory = root / "current"
            older_directory = root / "older"
            package_directory.mkdir()
            current_directory.mkdir()
            older_directory.mkdir()
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
                archive.extractall(current_directory, filter="data")
            with tarfile.open(tarball) as archive:
                archive.extractall(older_directory, filter="data")
            current_package = current_directory / "package"
            older_package = older_directory / "package"
            older_metadata_path = older_package / "package.json"
            older_metadata = json.loads(older_metadata_path.read_text(encoding="utf-8"))
            older_metadata["version"] = "0.1.5"
            older_metadata_path.write_text(
                f"{json.dumps(older_metadata, indent=2)}\n",
                encoding="utf-8",
            )
            older_manager = older_package / "manager/AGENTS.md"
            older_manager.write_text(
                f"{older_manager.read_text(encoding='utf-8')}\nOLDER PACKAGE BYTES\n",
                encoding="utf-8",
            )
            current_cli = current_package / "bin/herdr-orchestrator.mjs"
            older_cli = older_package / "bin/herdr-orchestrator.mjs"

            def initialize_project(project: Path) -> None:
                initialized = subprocess.run(
                    ["git", "init", "--quiet", str(project)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(initialized.returncode, 0, initialized.stderr)

            def run_cli(
                cli: Path,
                project: Path,
                *,
                interrupt_after: int | None = None,
            ) -> subprocess.CompletedProcess[str]:
                environment = os.environ.copy()
                if interrupt_after is not None:
                    environment.update(
                        {
                            "NODE_ENV": "test",
                            "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION": str(
                                interrupt_after
                            ),
                        }
                    )
                return subprocess.run(
                    [
                        "node",
                        str(cli),
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
                    env=environment,
                    timeout=30,
                )

            probe = root / "probe"
            initialize_project(probe)
            published = run_cli(older_cli, probe, interrupt_after=2)
            self.assertEqual(published.returncode, 86, published.stderr)
            probe_journal = json.loads(
                (probe / ".herdr-orchestrator/install-journal.json").read_text(encoding="utf-8")
            )
            manager_operation = next(
                index
                for index, operation in enumerate(probe_journal["operations"])
                if operation["target"].get("path") == ".herdr-orchestrator/manager/AGENTS.md"
            )
            manager_target_mutation = 4 + manager_operation * 4

            project = root / "project"
            initialize_project(project)
            interrupted = run_cli(
                older_cli,
                project,
                interrupt_after=manager_target_mutation,
            )
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            active_journal = json.loads(
                (project / ".herdr-orchestrator/install-journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(active_journal["package_version"], "0.1.5")
            installed_manager = project / ".herdr-orchestrator/manager/AGENTS.md"
            self.assertIn("OLDER PACKAGE BYTES", installed_manager.read_text(encoding="utf-8"))

            recovered = run_cli(current_cli, project)

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertFalse((project / ".herdr-orchestrator/install-journal.json").exists())
            manifest = json.loads(
                (project / ".herdr-orchestrator/manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["version"], __version__)
            self.assertEqual(
                installed_manager.read_bytes(),
                (current_package / "manager/AGENTS.md").read_bytes(),
            )

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

    def _run(
        self,
        *arguments: str,
        env: dict[str, str] | None = None,
        cwd: Path = REPO_ROOT,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["node", str(CLI), *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=30,
        )

    def _git_status(self, project: Path) -> str:
        result = subprocess.run(
            ["git", "-C", str(project), "status", "--short"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


if __name__ == "__main__":
    unittest.main()
