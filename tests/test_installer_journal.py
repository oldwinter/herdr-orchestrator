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

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_FAULT_LOADER = REPO_ROOT / "tests/installer_fault_loader.mjs"
INSTALLER_FAULT_ENV = {
    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION",
    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL",
    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX",
}


class InstallerJournalPackedTests(unittest.TestCase):
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
            self.assertNotIn(
                "package/tests/installer_fault_adapter.mjs",
                packaged_files,
            )
            self.assertNotIn(
                "package/tests/installer_fault_loader.mjs",
                packaged_files,
            )
            journal_source = (extracted / "package/bin/installer-journal.mjs").read_text(
                encoding="utf-8"
            )
            for forbidden in (
                "HERDR_ORCHESTRATOR_TEST",
                "TEST_ADAPTER",
                "installer-test-adapter",
                "durableMutation",
                "beforeJournalClaim",
            ):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, journal_source)
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
                extra_environment: dict[str, str] | None = None,
                interrupt_after: int | None = None,
            ) -> subprocess.CompletedProcess[str]:
                environment = os.environ.copy()
                if extra_environment is not None:
                    environment.update(extra_environment)
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
                        *self._node_command(packed_cli, environment),
                        *arguments,
                        "--project",
                        str(project),
                    ],
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

            def assert_recovered(
                project: Path,
                operation: str,
                expected_snapshot: dict[str, object],
                context: str,
            ) -> None:
                doctor = run_cli(
                    project,
                    ["doctor"],
                    extra_environment={"PYTHON": "/bin/false"},
                )
                self.assertEqual(doctor.returncode, 1, doctor.stderr)
                installation = json.loads(doctor.stdout)["installation"]
                self.assertFalse(
                    installation["journal"]["active"],
                    f"{context} left an active journal",
                )
                if operation == "uninstall":
                    self.assertFalse(installation["manifest"])
                else:
                    self.assertTrue(installation["ok"])
                self.assertEqual(
                    snapshot(project),
                    expected_snapshot,
                    f"{context} failed to converge",
                )

            mutation_counts: dict[str, int] = {}
            recovery_mutation_counts: dict[str, int] = {}
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
                        assert_recovered(
                            project,
                            operation,
                            expected_snapshot,
                            f"{operation} initial mutation {interrupt_after}",
                        )
                    else:
                        self.fail(f"{operation} exceeded the mutation-matrix bound")

                    for interrupt_after in range(1, 161):
                        project = root / f"recovery-{operation}-{interrupt_after}"
                        setup_project(project, operation)
                        interrupted_base = run_cli(
                            project,
                            operation_arguments(operation),
                            extra_environment={
                                "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": ("journal:published")
                            },
                        )
                        self.assertEqual(
                            interrupted_base.returncode,
                            86,
                            interrupted_base.stderr,
                        )
                        self.assertTrue(
                            (project / ".herdr-orchestrator/install-journal.json").is_file()
                        )
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
                            assert_recovered(
                                project,
                                operation,
                                expected_snapshot,
                                f"{operation} terminal recovery mutation " f"{interrupt_after}",
                            )
                            recovery_mutation_counts[operation] = interrupt_after - 1
                            break

                        recovered = run_cli(
                            project,
                            operation_arguments(operation),
                        )
                        self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        assert_recovered(
                            project,
                            operation,
                            expected_snapshot,
                            f"{operation} recovery mutation {interrupt_after}",
                        )
                    else:
                        self.fail(f"{operation} recovery exceeded the mutation-matrix bound")

            self.assertGreater(mutation_counts["install"], 20)
            self.assertGreater(mutation_counts["upgrade"], 4)
            self.assertGreater(mutation_counts["uninstall"], 15)
            self.assertGreater(recovery_mutation_counts["install"], 20)
            self.assertGreater(recovery_mutation_counts["upgrade"], 5)
            self.assertGreater(recovery_mutation_counts["uninstall"], 15)

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
                interrupt_label: str | None = None,
                interrupt_label_prefix: str | None = None,
            ) -> subprocess.CompletedProcess[str]:
                environment = os.environ.copy()
                if interrupt_label is not None:
                    environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL"] = interrupt_label
                if interrupt_label_prefix is not None:
                    environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX"] = (
                        interrupt_label_prefix
                    )
                return subprocess.run(
                    [
                        *self._node_command(cli, environment),
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
            published = run_cli(
                older_cli,
                probe,
                interrupt_label="journal:published",
            )
            self.assertEqual(published.returncode, 86, published.stderr)
            probe_journal = json.loads(
                (probe / ".herdr-orchestrator/install-journal.json").read_text(encoding="utf-8")
            )
            manager_operation = next(
                index
                for index, operation in enumerate(probe_journal["operations"])
                if operation["target"].get("path") == ".herdr-orchestrator/manager/AGENTS.md"
            )
            manager_target_label = f"target:operation-{manager_operation + 1}:"

            project = root / "project"
            initialize_project(project)
            interrupted = run_cli(
                older_cli,
                project,
                interrupt_label_prefix=manager_target_label,
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


if __name__ == "__main__":
    unittest.main()
