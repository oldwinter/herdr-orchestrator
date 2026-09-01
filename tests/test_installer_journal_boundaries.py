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
            workflow.chmod(0o640)
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
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._node_command(env), *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
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
