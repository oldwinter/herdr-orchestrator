from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_distribution import REPO_ROOT, DistributionCliFixture


class DistributionManagerTests(DistributionCliFixture, unittest.TestCase):
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

    def test_manager_rejects_explicit_harnesses_outside_the_manager_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            probe = root / "manager-probe"
            for name in ("grok", "codex", "claude"):
                harness = commands / name
                harness.write_text(
                    "#!/bin/sh\n" f'printf "{name}\\n" >> "$MANAGER_PROBE"\n',
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

            for name in ("droid", "pi", "hermes"):
                manager = self._run("manager", name, env=environment)
                self.assertEqual(manager.returncode, 2, name)
                self.assertEqual(
                    manager.stderr.strip(),
                    f"manager_harness_unsupported: {name}",
                )
            manager = self._run("manager", "--harness", "droid", env=environment)
            self.assertEqual(manager.returncode, 2)
            self.assertEqual(
                manager.stderr.strip(),
                "manager_harness_unsupported: droid",
            )
            manager = self._run("manager", "bogus", env=environment)
            self.assertEqual(manager.returncode, 2)
            self.assertEqual(manager.stderr.strip(), "unsupported_harness: bogus")

            for name in ("grok", "codex", "claude"):
                manager = self._run("manager", name, env=environment)
                self.assertEqual(manager.returncode, 0, manager.stderr)
            self.assertEqual(
                probe.read_text(encoding="utf-8").splitlines(),
                ["grok", "codex", "claude"],
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


if __name__ == "__main__":
    unittest.main()
