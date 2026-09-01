from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class QualityCommandTests(unittest.TestCase):
    def test_installer_crash_matrix_is_enforced_once_outside_repeated_gates(self) -> None:
        justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
        stability = (REPO_ROOT / "scripts/test_stability.py").read_text(encoding="utf-8")
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        installer_tests = (REPO_ROOT / "tests/test_installer_journal.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("test-installer-crash-matrix:", justfile)
        self.assertEqual(justfile.count("@just test-installer-crash-matrix"), 1)
        self.assertIn('-m "not installer_crash_matrix"', justfile)
        self.assertIn('"not installer_crash_matrix"', stability)
        self.assertIn("@installer_crash_matrix", installer_tests)
        self.assertIn("- name: Installer crash matrix", workflow)
        self.assertIn("id: installer-matrix", workflow)
        self.assertIn("INSTALLER_MATRIX: ${{ steps.installer-matrix.outcome }}", workflow)
        self.assertIn('test "$INSTALLER_MATRIX" = success', workflow)

    def test_security_skips_editable_package_and_propagates_pip_audit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            probe = root / "uv-probe.jsonl"
            uv = commands / "uv"
            uv.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "with open(os.environ['QUALITY_PROBE'], 'a', encoding='utf-8') as output:\n"
                "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "if sys.argv[1:3] == ['run', 'pip-audit']:\n"
                "    raise SystemExit(int(os.environ['PIP_AUDIT_EXIT']))\n",
                encoding="utf-8",
            )
            uv.chmod(0o755)
            npm = commands / "npm"
            npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            npm.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                    "PIP_AUDIT_EXIT": "37",
                    "QUALITY_PROBE": str(probe),
                }
            )
            result = subprocess.run(
                ["just", "security"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            invocations = [
                json.loads(line) for line in probe.read_text(encoding="utf-8").splitlines()
            ]
            pip_audit_invocations = [
                arguments for arguments in invocations if arguments[0:2] == ["run", "pip-audit"]
            ]

        self.assertEqual(len(pip_audit_invocations), 1)
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertIn("--local", pip_audit_invocations[0])
        self.assertIn("--skip-editable", pip_audit_invocations[0])

    def test_profile_tests_propagates_pytest_failure(self) -> None:
        environment = os.environ.copy()
        environment["PYTEST_ADDOPTS"] = "--definitely-invalid"

        result = subprocess.run(
            ["just", "profile-tests"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
