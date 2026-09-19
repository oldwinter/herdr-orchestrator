from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

from herdr_orchestrator import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin/herdr-orchestrator.mjs"
MANAGER_PACKAGE = REPO_ROOT / "packages/herdr-manager"
INSTALLER_FAULT_LOADER = REPO_ROOT / "tests/installer_fault_loader.mjs"
INSTALLER_FAULT_ENV = {
    "HERDR_ORCHESTRATOR_TEST_FAIL_ON_JOURNAL_CLAIM",
    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION",
    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL",
    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX",
    "HERDR_ORCHESTRATOR_TEST_JOURNAL_CLAIM_BARRIER",
    "HERDR_ORCHESTRATOR_TEST_PAUSE_AT_LABEL_PREFIX",
    "HERDR_ORCHESTRATOR_TEST_PAUSE_BARRIER",
    "HERDR_ORCHESTRATOR_TEST_REWRITE_AFTER_MUTATION",
    "HERDR_ORCHESTRATOR_TEST_REWRITE_AT_LABEL_PREFIX",
    "HERDR_ORCHESTRATOR_TEST_PLANNING_BARRIER",
    "HERDR_ORCHESTRATOR_TEST_UNINSTALL_EXCLUDE_BARRIER",
    "HERDR_ORCHESTRATOR_TEST_UNINSTALL_TRANSACTION_BARRIER",
}


def npm_pack_entry(stdout: str) -> dict[str, object]:
    payload = json.loads(stdout)
    if isinstance(payload, list):
        if len(payload) != 1:
            raise AssertionError("npm pack returned an unexpected number of packages")
        payload = payload[0]
    if isinstance(payload, dict) and "herdr-orchestrator" in payload:
        payload = payload["herdr-orchestrator"]
    if not isinstance(payload, dict):
        raise AssertionError("npm pack returned an unexpected package shape")
    return payload


class DistributionCliMixin:
    def _run(
        self,
        *arguments: str,
        env: dict[str, str] | None = None,
        cwd: Path = REPO_ROOT,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._node_command(CLI, env), *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=30,
        )

    def _node_command(
        self,
        cli: Path,
        env: dict[str, str] | None,
    ) -> list[str]:
        command = ["node"]
        if env is not None and INSTALLER_FAULT_ENV.intersection(env):
            command.extend(["--no-warnings", "--loader", str(INSTALLER_FAULT_LOADER)])
        command.append(str(cli))
        return command

    def _git_status(self, project: Path) -> str:
        result = subprocess.run(
            ["git", "-C", str(project), "status", "--short"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


class DistributionCliTests(DistributionCliMixin, unittest.TestCase):
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

    def test_local_runtime_tarball_matches_its_own_pack_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packed = subprocess.run(
                [
                    "npm",
                    "pack",
                    "--json",
                    "--pack-destination",
                    temporary,
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(packed.returncode, 0, packed.stderr)
            pack = npm_pack_entry(packed.stdout)
            tarball = Path(temporary) / pack["filename"]
            integrity = "sha512-" + base64.b64encode(
                hashlib.sha512(tarball.read_bytes()).digest()
            ).decode("ascii")

        self.assertEqual(integrity, pack["integrity"])

    def test_npm_pack_entry_accepts_object_and_array_json_shapes(self) -> None:
        package = {"filename": "herdr-orchestrator-0.1.7.tgz"}
        for payload in (
            {"herdr-orchestrator": package},
            [package],
        ):
            with self.subTest(payload_type=type(payload).__name__):
                self.assertEqual(npm_pack_entry(json.dumps(payload)), package)

    def test_npm_dependency_audit_covers_both_package_lockfiles(self) -> None:
        security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
        quality_bundle = (REPO_ROOT / "scripts/quality_bundle.py").read_text(encoding="utf-8")

        self.assertIn("npm audit --package-lock-only", security)
        self.assertIn("packages/herdr-manager", security)
        self.assertIn("quality_bundle.py run --producer security", justfile)
        self.assertIn('NPM_AUDIT, "--json"', quality_bundle)
        self.assertIn('NPM_AUDIT, "--prefix", "packages/herdr-manager"', quality_bundle)
        self.assertIn('"packages/herdr-manager"', quality_bundle)

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

    def test_uninstall_without_a_manifest_preserves_caller_empty_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            caller_directory = project / ".orchestrator"
            caller_directory.mkdir()

            uninstall = self._run("uninstall", "--project", str(project))

            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertTrue(caller_directory.is_dir())

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



if __name__ == "__main__":
    unittest.main()
