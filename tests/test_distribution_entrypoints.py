from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin/herdr-orchestrator.mjs"


class DistributionEntrypointTests(unittest.TestCase):
    def test_npm_infers_orchestrator_for_a_packed_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / ".git").mkdir()
            packed = subprocess.run(
                ["npm", "pack", "--json", "--pack-destination", str(root)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(packed.returncode, 0, packed.stderr)
            payload = json.loads(packed.stdout)
            package = payload[0] if isinstance(payload, list) else payload["herdr-orchestrator"]
            tarball = root / package["filename"]
            result = subprocess.run(
                [
                    "npm",
                    "exec",
                    "--offline",
                    "--yes",
                    "--ignore-scripts",
                    "--cache",
                    str(root / "cache"),
                    "--",
                    f"file:{tarball}",
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "grok",
                    "--install-skill",
                ],
                cwd=project,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])
            self.assertTrue((project / ".herdr-orchestrator/manifest.json").is_file())

    def test_installed_skill_pins_runtime_without_shadowing_native_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".git").mkdir()
            install = subprocess.run(
                [
                    "node",
                    str(CLI),
                    "install",
                    "--project",
                    str(project),
                    "--harness",
                    "grok",
                    "--install-skill",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            skill = (project / ".agents/skills/herdr-orchestrator/SKILL.md").read_text(
                encoding="utf-8"
            )
            blocks = skill.split("```bash\n")[1:]
            helpers = [block.split("```", 1)[0] for block in blocks if "HERDR_VERSION=" in block]
            self.assertEqual(len(helpers), 1, "installed Skill must define one versioned helper")
            commands = project / "bin"
            commands.mkdir()
            npm_probe = project / "npm-args"
            for name, script in {
                "npm": '#!/bin/sh\nprintf "%s\\n" "$@" > "$NPM_PROBE"\n',
                "herdr": '#!/bin/sh\ntest "$1 $2" = "agent list" && printf native-agent-list\n',
            }.items():
                command = commands / name
                command.write_text(script, encoding="utf-8")
                command.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                NPM_PROBE=str(npm_probe), PATH=f"{commands}{os.pathsep}{environment['PATH']}"
            )
            result = subprocess.run(
                [
                    "bash",
                    "-eu",
                    "-c",
                    helpers[0] + "\nherdr_orchestrator status --project .\nherdr agent list\n",
                ],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "native-agent-list")
            self.assertEqual(
                npm_probe.read_text(encoding="utf-8").splitlines(),
                [
                    "exec",
                    "--yes",
                    f"--package=herdr-orchestrator@{__version__}",
                    "--",
                    "herdr-orchestrator",
                    "status",
                    "--project",
                    ".",
                ],
            )
