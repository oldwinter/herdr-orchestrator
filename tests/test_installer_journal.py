from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from herdr_orchestrator import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_FAULT_LOADER = REPO_ROOT / "tests/installer_fault_loader.mjs"
INSTALLER_FAULT_ENV = {
    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION",
    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL",
    "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX",
    "HERDR_ORCHESTRATOR_TEST_MUTATION_LOG",
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

            def relocate_active_journal(project: Path) -> None:
                journal_directory = project / ".herdr-orchestrator"
                journal_path = journal_directory / "install-journal.json"
                owner_paths = list(journal_directory.glob(".install-journal.*.owner"))
                self.assertEqual(len(owner_paths), 1)
                journal_paths = [*owner_paths]
                if journal_path.is_file():
                    journal_paths.append(journal_path)
                exclude_path = str((project / ".git/info/exclude").resolve())
                for path in journal_paths:
                    mode = path.stat().st_mode & 0o7777
                    journal = json.loads(path.read_text(encoding="utf-8"))
                    for inventory_name in ("prior_inventory", "desired_inventory"):
                        relocated_inventory: dict[str, object] = {}
                        for item in journal[inventory_name].values():
                            target = item["target"]
                            if target["scope"] == "git-exclude":
                                target["path"] = exclude_path
                            relocated_inventory[f"{target['scope']}:{target['path']}"] = item
                        journal[inventory_name] = relocated_inventory
                    for operation_item in journal["operations"]:
                        target = operation_item["target"]
                        if target["scope"] == "git-exclude":
                            target["path"] = exclude_path
                    path.write_text(
                        f"{json.dumps(journal, indent=2)}\n",
                        encoding="utf-8",
                    )
                    path.chmod(mode)

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

            def unique_mutation_cutoffs(
                labels: list[str],
                project: Path,
            ) -> list[tuple[int, str]]:
                project_prefix = str(project.resolve())
                seen: set[str] = set()
                cutoffs: list[tuple[int, str]] = []
                for index, label in enumerate(labels, start=1):
                    normalized = label.replace(project_prefix, "<project>")
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    cutoffs.append((index, normalized))
                return cutoffs

            mutation_counts: dict[str, int] = {}
            recovery_mutation_counts: dict[str, int] = {}
            for operation in ("install", "upgrade", "uninstall"):
                with self.subTest(operation=operation):
                    template = root / f"template-{operation}"
                    setup_project(template, operation)
                    expected_project = root / f"expected-{operation}"
                    shutil.copytree(template, expected_project)
                    initial_mutation_log = root / f"initial-labels-{operation}.txt"
                    expected_run = run_cli(
                        expected_project,
                        operation_arguments(operation),
                        extra_environment={
                            "HERDR_ORCHESTRATOR_TEST_MUTATION_LOG": str(initial_mutation_log)
                        },
                    )
                    self.assertEqual(expected_run.returncode, 0, expected_run.stderr)
                    expected_snapshot = snapshot(expected_project)
                    initial_labels = initial_mutation_log.read_text(encoding="utf-8").splitlines()
                    self.assertGreater(len(initial_labels), 0)
                    initial_cutoffs = unique_mutation_cutoffs(
                        initial_labels,
                        expected_project,
                    )

                    def run_initial_cutoff(
                        cutoff: tuple[int, str],
                        *,
                        operation: str = operation,
                        template: Path = template,
                        expected_snapshot: dict[str, object] = expected_snapshot,
                    ) -> None:
                        interrupt_after, mutation_label = cutoff
                        project = root / f"{operation}-{interrupt_after}"
                        shutil.copytree(template, project)
                        interrupted = run_cli(
                            project,
                            operation_arguments(operation),
                            interrupt_after=interrupt_after,
                        )
                        self.assertEqual(
                            interrupted.returncode,
                            86,
                            f"{mutation_label}: {interrupted.stderr}",
                        )
                        recovered = run_cli(
                            project,
                            operation_arguments(operation),
                        )
                        self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        assert_recovered(
                            project,
                            operation,
                            expected_snapshot,
                            f"{operation} initial mutation {mutation_label}",
                        )

                    with ThreadPoolExecutor(
                        max_workers=min(8, len(initial_cutoffs)),
                    ) as executor:
                        list(executor.map(run_initial_cutoff, initial_cutoffs))
                    mutation_counts[operation] = len(initial_cutoffs)

                    recovery_base = root / f"recovery-base-{operation}"
                    shutil.copytree(template, recovery_base)
                    interrupted_base = run_cli(
                        recovery_base,
                        operation_arguments(operation),
                        extra_environment={
                            "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": (
                                "temporary:journal:owner:created"
                            )
                        },
                    )
                    self.assertEqual(
                        interrupted_base.returncode,
                        86,
                        interrupted_base.stderr,
                    )
                    self.assertEqual(
                        len(
                            list(
                                (recovery_base / ".herdr-orchestrator").glob(
                                    ".install-journal.*.owner"
                                )
                            )
                        ),
                        1,
                    )
                    self.assertFalse(
                        (recovery_base / ".herdr-orchestrator/install-journal.json").exists()
                    )
                    recovery_discovery_project = root / f"recovery-discovery-{operation}"
                    shutil.copytree(recovery_base, recovery_discovery_project)
                    relocate_active_journal(recovery_discovery_project)
                    recovery_mutation_log = root / f"recovery-labels-{operation}.txt"
                    recovery_discovery = run_cli(
                        recovery_discovery_project,
                        operation_arguments(operation),
                        extra_environment={
                            "HERDR_ORCHESTRATOR_TEST_MUTATION_LOG": str(recovery_mutation_log)
                        },
                    )
                    self.assertEqual(
                        recovery_discovery.returncode,
                        0,
                        recovery_discovery.stderr,
                    )
                    assert_recovered(
                        recovery_discovery_project,
                        operation,
                        expected_snapshot,
                        f"{operation} recovery label discovery",
                    )
                    recovery_labels = recovery_mutation_log.read_text(encoding="utf-8").splitlines()
                    self.assertIn("journal:published:recovered", recovery_labels)
                    recovery_cutoffs = unique_mutation_cutoffs(
                        recovery_labels,
                        recovery_discovery_project,
                    )

                    def run_recovery_cutoff(
                        cutoff: tuple[int, str],
                        *,
                        operation: str = operation,
                        recovery_base: Path = recovery_base,
                        expected_snapshot: dict[str, object] = expected_snapshot,
                    ) -> None:
                        interrupt_after, mutation_label = cutoff
                        project = root / f"recovery-{operation}-{interrupt_after}"
                        shutil.copytree(recovery_base, project)
                        relocate_active_journal(project)
                        interrupted = run_cli(
                            project,
                            operation_arguments(operation),
                            interrupt_after=interrupt_after,
                        )
                        self.assertEqual(
                            interrupted.returncode,
                            86,
                            f"{mutation_label}: {interrupted.stderr}",
                        )
                        recovered = run_cli(
                            project,
                            operation_arguments(operation),
                        )
                        self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        assert_recovered(
                            project,
                            operation,
                            expected_snapshot,
                            f"{operation} recovery mutation {mutation_label}",
                        )

                    with ThreadPoolExecutor(
                        max_workers=min(8, len(recovery_cutoffs)),
                    ) as executor:
                        list(executor.map(run_recovery_cutoff, recovery_cutoffs))
                    recovery_mutation_counts[operation] = len(recovery_cutoffs)

                    recovered_publication_project = root / f"recovered-publication-{operation}"
                    shutil.copytree(
                        recovery_base,
                        recovered_publication_project,
                    )
                    relocate_active_journal(recovered_publication_project)
                    recovered_publication = run_cli(
                        recovered_publication_project,
                        operation_arguments(operation),
                        extra_environment={
                            "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": (
                                "journal:published:recovered"
                            )
                        },
                    )
                    self.assertEqual(
                        recovered_publication.returncode,
                        86,
                        recovered_publication.stderr,
                    )
                    self.assertTrue(
                        (
                            recovered_publication_project
                            / ".herdr-orchestrator/install-journal.json"
                        ).is_file()
                    )
                    recovered = run_cli(
                        recovered_publication_project,
                        operation_arguments(operation),
                    )
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    assert_recovered(
                        recovered_publication_project,
                        operation,
                        expected_snapshot,
                        f"{operation} recovered publication chain",
                    )

                    temporary_removal_project = root / f"temporary-removal-{operation}"
                    shutil.copytree(template, temporary_removal_project)
                    published = run_cli(
                        temporary_removal_project,
                        operation_arguments(operation),
                        extra_environment={
                            "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL": ("journal:published")
                        },
                    )
                    self.assertEqual(published.returncode, 86, published.stderr)
                    progress_temporary = run_cli(
                        temporary_removal_project,
                        operation_arguments(operation),
                        extra_environment={
                            "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX": (
                                "temporary:journal:"
                            )
                        },
                    )
                    self.assertEqual(
                        progress_temporary.returncode,
                        86,
                        progress_temporary.stderr,
                    )
                    self.assertTrue(
                        list(
                            (temporary_removal_project / ".herdr-orchestrator").glob(
                                ".install-journal.*.tmp"
                            )
                        )
                    )
                    temporary_removed = run_cli(
                        temporary_removal_project,
                        operation_arguments(operation),
                        extra_environment={
                            "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX": (
                                "journal:temporary:removed:"
                            )
                        },
                    )
                    self.assertEqual(
                        temporary_removed.returncode,
                        86,
                        temporary_removed.stderr,
                    )
                    self.assertFalse(
                        list(
                            (temporary_removal_project / ".herdr-orchestrator").glob(
                                ".install-journal.*.tmp"
                            )
                        )
                    )
                    recovered = run_cli(
                        temporary_removal_project,
                        operation_arguments(operation),
                    )
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    assert_recovered(
                        temporary_removal_project,
                        operation,
                        expected_snapshot,
                        f"{operation} temporary removal chain",
                    )

            self.assertGreater(mutation_counts["install"], 20)
            self.assertGreater(mutation_counts["upgrade"], 4)
            self.assertGreater(mutation_counts["uninstall"], 15)
            self.assertGreater(recovery_mutation_counts["install"], 20)
            self.assertGreater(recovery_mutation_counts["upgrade"], 5)
            self.assertGreater(recovery_mutation_counts["uninstall"], 15)

    def test_current_package_recovers_pre_owner_pre_mode_v1_journals(self) -> None:
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
                archive.extractall(extracted, filter="data")
            packed_cli = extracted / "package/bin/herdr-orchestrator.mjs"

            def run_cli(
                project: Path,
                arguments: list[str],
                *,
                extra_environment: dict[str, str] | None = None,
                interrupt: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                environment = os.environ.copy()
                if extra_environment is not None:
                    environment.update(extra_environment)
                if interrupt:
                    environment["HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL"] = "journal:published"
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

            def setup(project: Path, operation: str) -> None:
                initialized = subprocess.run(
                    ["git", "init", "--quiet", str(project)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(initialized.returncode, 0, initialized.stderr)
                if operation in {"upgrade", "uninstall"}:
                    installed = run_cli(
                        project,
                        ["install", "--harness", "droid"],
                    )
                    self.assertEqual(installed.returncode, 0, installed.stderr)

            def strip_manifest_modes(project: Path) -> None:
                manifest_path = project / ".herdr-orchestrator/manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.pop("file_modes", None)
                manifest_path.write_text(
                    f"{json.dumps(manifest, indent=2)}\n",
                    encoding="utf-8",
                )

            def arguments(operation: str) -> list[str]:
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
                files = {
                    path.relative_to(project).as_posix(): (
                        path.stat().st_mode & 0o7777,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                    for root_name in (
                        ".herdr-orchestrator",
                        ".orchestrator",
                        ".agents/skills/herdr-orchestrator",
                    )
                    for path in sorted((project / root_name).rglob("*"))
                    if path.is_file()
                }
                return {
                    "exclude": (project / ".git/info/exclude").read_bytes(),
                    "files": files,
                }

            def prepare_legacy_input(project: Path, operation: str) -> None:
                if operation in {"upgrade", "uninstall"}:
                    strip_manifest_modes(project)
                if operation == "upgrade":
                    (project / ".herdr-orchestrator/workflows/multi-harness.toml").chmod(0o600)

            def downgrade(project: Path, claim_state: str) -> None:
                directory = project / ".herdr-orchestrator"
                journal_path = directory / "install-journal.json"
                owner = next(directory.glob(".install-journal.*.owner"))
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                for inventory_name in ("prior_inventory", "desired_inventory"):
                    for item in journal[inventory_name].values():
                        item["state"].pop("mode", None)
                for operation_item in journal["operations"]:
                    operation_item["original"].pop("mode", None)
                    operation_item["desired"].pop("mode", None)
                    if (
                        operation_item["target"].get("path") == ".herdr-orchestrator/manifest.json"
                        and operation_item["desired"].get("kind") == "regular"
                    ):
                        content = base64.b64decode(operation_item["desired_content_base64"])
                        manifest = json.loads(content)
                        manifest.pop("file_modes", None)
                        content = f"{json.dumps(manifest, indent=2)}\n".encode()
                        digest = hashlib.sha256(content).hexdigest()
                        operation_item["desired"]["digest"] = digest
                        operation_item["desired_content_base64"] = base64.b64encode(
                            content
                        ).decode()
                        journal["desired_inventory"]["project:.herdr-orchestrator/manifest.json"][
                            "state"
                        ]["digest"] = digest
                journal_path.write_text(
                    f"{json.dumps(journal, indent=2)}\n",
                    encoding="utf-8",
                )
                if claim_state == "published-only":
                    owner.unlink()
                else:
                    owner.rename(owner.with_name(owner.name.removesuffix(".owner") + ".tmp"))
                if claim_state == "temporary-only":
                    journal_path.unlink()

            for operation in ("install", "upgrade", "uninstall"):
                expected_project = root / f"expected-{operation}"
                setup(expected_project, operation)
                prepare_legacy_input(expected_project, operation)
                expected = run_cli(expected_project, arguments(operation))
                self.assertEqual(expected.returncode, 0, expected.stderr)
                expected_snapshot = snapshot(expected_project)
                for claim_state in (
                    "published-and-temporary",
                    "temporary-only",
                    "published-only",
                ):
                    with self.subTest(
                        operation=operation,
                        claim_state=claim_state,
                    ):
                        project = root / f"legacy-{operation}-{claim_state}"
                        setup(project, operation)
                        prepare_legacy_input(project, operation)
                        interrupted = run_cli(
                            project,
                            arguments(operation),
                            interrupt=True,
                        )
                        self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
                        downgrade(project, claim_state)

                        recovered = run_cli(project, arguments(operation))

                        self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        doctor = run_cli(
                            project,
                            ["doctor"],
                            extra_environment={"PYTHON": "/bin/false"},
                        )
                        self.assertEqual(doctor.returncode, 1, doctor.stderr)
                        installation = json.loads(doctor.stdout)["installation"]
                        self.assertFalse(installation["journal"]["active"])
                        self.assertEqual(snapshot(project), expected_snapshot)
                        repeated = run_cli(project, arguments(operation))
                        self.assertEqual(repeated.returncode, 0, repeated.stderr)
                        self.assertEqual(snapshot(project), expected_snapshot)

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
