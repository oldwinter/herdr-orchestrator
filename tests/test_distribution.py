from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
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


def require_node() -> str:
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("node is required for CLI subprocess tests")
    return node


def require_npm() -> None:
    if shutil.which("npm") is None:
        raise unittest.SkipTest("npm is required for package tests")


class DistributionCliFixture:
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
        command = [require_node()]
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


class DistributionCliTests(DistributionCliFixture, unittest.TestCase):
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
        require_npm()
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
        require_npm()
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

    def test_packed_herdr_manager_package_runs_outside_the_source_checkout(self) -> None:
        require_npm()
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
