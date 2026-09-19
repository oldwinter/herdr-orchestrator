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
