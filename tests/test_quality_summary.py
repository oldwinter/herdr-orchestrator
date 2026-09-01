from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SUMMARY_SCRIPT = REPO_ROOT / "scripts/quality_summary.py"
BUNDLE_SCRIPT = REPO_ROOT / "scripts/quality_bundle.py"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


quality_bundle = _load_script("quality_bundle_summary_fixture", BUNDLE_SCRIPT)
quality_summary = _load_script("quality_summary_regression", SUMMARY_SCRIPT)


class QualitySummaryTests(unittest.TestCase):
    def test_manifest_argument_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SUMMARY_SCRIPT),
                    "--output",
                    str(Path(temporary) / "summary.md"),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--manifest", result.stderr)

    def test_missing_security_artifacts_are_not_verified_not_zero(self) -> None:
        status, summary = self._run_summary(security_files={})

        self.assertEqual(status, 1)
        self.assertIn("Security: **NOT VERIFIED**", summary)
        self.assertNotIn("**0** medium/high Bandit findings", summary)
        self.assertNotIn("**0** dependency vulnerabilities", summary)

    def test_failed_security_command_is_not_verified_not_zero(self) -> None:
        status, summary = self._run_summary(
            security_files=self._complete_security(),
            security_exit=17,
        )

        self.assertEqual(status, 1)
        self.assertIn("Security: **NOT VERIFIED**", summary)
        self.assertNotIn("**0** medium/high Bandit findings", summary)

    def test_unproven_empty_security_payloads_are_not_verified(self) -> None:
        status, summary = self._run_summary(
            security_files={
                "bandit.json": {"errors": [], "results": []},
                "pip-audit.json": {"dependencies": [], "fixes": []},
            }
        )

        self.assertEqual(status, 1)
        self.assertIn("Security: **NOT VERIFIED**", summary)
        self.assertNotIn("**0** medium/high Bandit findings", summary)

    def test_complete_zero_finding_security_payloads_pass(self) -> None:
        status, summary = self._run_summary(security_files=self._complete_security())

        self.assertEqual(status, 0, summary)
        self.assertIn("**0** medium/high Bandit findings", summary)
        self.assertIn("**0** dependency vulnerabilities", summary)

    def test_wrong_manifest_commit_is_not_verified(self) -> None:
        status, summary = self._run_summary(
            security_files=self._complete_security(),
            expected_commit="b" * 40,
        )

        self.assertEqual(status, 1)
        self.assertIn("NOT VERIFIED", summary)
        self.assertIn("quality_commit_mismatch", summary)

    def test_stale_same_commit_invocation_is_not_verified(self) -> None:
        status, summary = self._run_summary(
            security_files=self._complete_security(),
            expected_invocation="newer-invocation",
        )

        self.assertEqual(status, 1)
        self.assertIn("NOT VERIFIED", summary)
        self.assertIn("quality_invocation_mismatch", summary)

    def test_wrong_run_id_is_not_verified(self) -> None:
        status, summary = self._run_summary(
            security_files=self._complete_security(),
            expected_run="wrong-run-id",
        )

        self.assertEqual(status, 1)
        self.assertIn("NOT VERIFIED", summary)
        self.assertIn("quality_run_mismatch", summary)

    def test_summary_reads_only_the_completed_manifest_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            specs = self._specs(self._complete_security())
            bundle = quality_bundle.run_quality(
                root=root / "quality",
                commit="c" * 40,
                invocation_id="manifest-only",
                specs=specs,
            )
            poisoned = root / "coverage.json"
            poisoned.write_text(
                json.dumps({"totals": {"percent_covered": 13.0}}),
                encoding="utf-8",
            )
            output = root / "summary.md"
            manifest_payload = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "quality_summary.py",
                        "--manifest",
                        str(bundle.manifest_path),
                        "--expected-commit",
                        "c" * 40,
                        "--expected-invocation",
                        "manifest-only",
                        "--expected-run",
                        bundle.path.name,
                        "--expected-source",
                        manifest_payload["source_digest"],
                        "--output",
                        str(output),
                    ],
                ),
                patch.object(
                    quality_summary.quality_bundle,
                    "PRODUCER_SPECS",
                    {spec.name: spec for spec in specs},
                ),
            ):
                status = quality_summary.main()
            summary = output.read_text(encoding="utf-8")

        self.assertEqual(status, 0, summary)
        self.assertIn("Coverage: **91.0%**", summary)
        self.assertNotIn("13.0%", summary)

    def test_missing_manifest_path_is_not_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "summary.md"
            with patch.object(
                sys,
                "argv",
                [
                    "quality_summary.py",
                    "--manifest",
                    str(Path(temporary) / "missing" / "manifest.json"),
                    "--expected-commit",
                    "a" * 40,
                    "--expected-invocation",
                    "missing",
                    "--expected-run",
                    "missing-run",
                    "--expected-source",
                    "0" * 64,
                    "--output",
                    str(output),
                ],
            ):
                status = quality_summary.main()

            summary = output.read_text(encoding="utf-8")

        self.assertEqual(status, 1)
        self.assertIn("NOT VERIFIED", summary)
        self.assertIn("quality_manifest_missing", summary)

    def test_failed_quality_producers_are_not_verified(self) -> None:
        status, summary = self._run_summary(
            security_files=self._complete_security(),
            coverage_exit=3,
            stability_exit=4,
            build_exit=5,
        )

        self.assertEqual(status, 1)
        self.assertIn("Coverage: **NOT VERIFIED**", summary)
        self.assertIn("Stability: **NOT VERIFIED**", summary)
        self.assertIn("Build: **NOT VERIFIED**", summary)

    def test_unbounded_coverage_value_is_not_verified(self) -> None:
        specs = list(self._specs(self._complete_security()))
        specs[1] = self._json_producer(
            "coverage",
            {
                "coverage.json": self._coverage_payload(10**1000),
                "tests.json": self._tests_payload(),
            },
            {"coverage": "coverage.json", "tests": "tests.json"},
        )
        commit = "d" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = quality_bundle.run_quality(
                root=root / "quality",
                commit=commit,
                invocation_id="unbounded-coverage",
                specs=tuple(specs),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            output = root / "summary.md"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "quality_summary.py",
                        "--manifest",
                        str(bundle.manifest_path),
                        "--expected-commit",
                        commit,
                        "--expected-invocation",
                        "unbounded-coverage",
                        "--expected-run",
                        bundle.path.name,
                        "--expected-source",
                        manifest["source_digest"],
                        "--output",
                        str(output),
                    ],
                ),
                patch.object(
                    quality_summary.quality_bundle,
                    "PRODUCER_SPECS",
                    {spec.name: spec for spec in specs},
                ),
            ):
                status = quality_summary.main()
            summary = output.read_text(encoding="utf-8")

        self.assertEqual(status, 1)
        self.assertIn("Evidence: **NOT VERIFIED**", summary)

    def _run_summary(
        self,
        *,
        security_files: dict[str, dict[str, object]],
        security_exit: int = 0,
        coverage_exit: int = 0,
        stability_exit: int = 0,
        build_exit: int = 0,
        expected_commit: str | None = None,
        expected_invocation: str | None = None,
        expected_run: str | None = None,
    ) -> tuple[int, str]:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            specs = self._specs(
                security_files,
                security_exit=security_exit,
                coverage_exit=coverage_exit,
                stability_exit=stability_exit,
                build_exit=build_exit,
            )
            bundle = quality_bundle.run_quality(
                root=root / "quality",
                commit=commit,
                invocation_id="summary-fixture",
                specs=specs,
            )
            output = root / "summary.md"
            manifest_payload = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "quality_summary.py",
                        "--manifest",
                        str(bundle.manifest_path),
                        "--expected-commit",
                        expected_commit or commit,
                        "--expected-invocation",
                        expected_invocation or "summary-fixture",
                        "--expected-run",
                        expected_run or bundle.path.name,
                        "--expected-source",
                        manifest_payload["source_digest"],
                        "--output",
                        str(output),
                    ],
                ),
                patch.object(
                    quality_summary.quality_bundle,
                    "PRODUCER_SPECS",
                    {spec.name: spec for spec in specs},
                ),
            ):
                status = quality_summary.main()
            return status, output.read_text(encoding="utf-8")

    @classmethod
    def _specs(
        cls,
        security_files: dict[str, dict[str, object]],
        *,
        security_exit: int = 0,
        coverage_exit: int = 0,
        stability_exit: int = 0,
        build_exit: int = 0,
    ) -> tuple[object, ...]:
        return (
            cls._json_producer("lint", {}, {}),
            cls._json_producer(
                "coverage",
                {
                    "coverage.json": cls._coverage_payload(91.0),
                    "tests.json": cls._tests_payload(),
                },
                {"coverage": "coverage.json", "tests": "tests.json"},
                exit_code=coverage_exit,
            ),
            cls._json_producer(
                "stability",
                {
                    "stability.json": {
                        "executions": [
                            {"exit_code": 0, "run": 1, "tests": 1, "duration_seconds": 0.1},
                            {"exit_code": 0, "run": 2, "tests": 1, "duration_seconds": 0.1},
                            {"exit_code": 0, "run": 3, "tests": 1, "duration_seconds": 0.1},
                        ],
                        "runs": 3,
                        "status": "passed",
                        "unstable": [],
                    }
                },
                {"stability": "stability.json"},
                exit_code=stability_exit,
            ),
            cls._json_producer(
                "security",
                security_files,
                {
                    "bandit": "bandit.json",
                    "pip-audit": "pip-audit.json",
                    "npm-audit-root": "npm-audit-root.json",
                    "npm-audit-manager": "npm-audit-manager.json",
                },
                exit_code=security_exit,
            ),
            cls._json_producer(
                "build",
                {
                    "build.json": {
                        "command": "fixture",
                        "duration_seconds": 0.1,
                        "entry_count": 1,
                        "exit_code": 0,
                        "package_size_bytes": 10,
                        "status": "passed",
                        "unpacked_size_bytes": 20,
                    }
                },
                {"build": "build.json"},
                exit_code=build_exit,
            ),
            cls._profile_producer(),
        )

    @staticmethod
    def _json_producer(
        name: str,
        files: dict[str, dict[str, object]],
        artifacts: dict[str, str],
        *,
        exit_code: int = 0,
    ):
        statements = [
            "import os",
            "from pathlib import Path",
            "root = Path(os.environ['QUALITY_OUTPUT_DIR'])",
        ]
        for filename, payload in files.items():
            statements.append(
                f"(root / {filename!r}).write_text({json.dumps(payload)!r}, encoding='utf-8')"
            )
        statements.append(f"raise SystemExit({exit_code})")
        return quality_bundle.ProducerSpec(
            name=name,
            commands=(
                quality_bundle.CommandSpec(
                    argv=(sys.executable, "-c", "; ".join(statements)),
                    tool="python",
                    version_argv=(sys.executable, "--version"),
                ),
            ),
            artifacts=tuple(
                quality_bundle.ArtifactSpec(key, filename, "json")
                for key, filename in artifacts.items()
            ),
        )

    @staticmethod
    def _complete_security() -> dict[str, dict[str, object]]:
        return {
            "bandit.json": {
                "errors": [],
                "generated_at": "2026-08-31T00:00:00Z",
                "metrics": {"_totals": {}},
                "results": [],
            },
            "pip-audit.json": {
                "dependencies": [{"name": "example", "version": "1.0.0", "vulns": []}],
                "fixes": [],
            },
            "npm-audit-root.json": {
                "auditReportVersion": 2,
                "metadata": {
                    "vulnerabilities": {"total": 0},
                    "dependencies": {"prod": 0, "dev": 0, "optional": 0},
                },
                "vulnerabilities": {},
            },
            "npm-audit-manager.json": {
                "auditReportVersion": 2,
                "metadata": {
                    "vulnerabilities": {"total": 0},
                    "dependencies": {"prod": 0, "dev": 0, "optional": 0},
                },
                "vulnerabilities": {},
            },
        }

    @staticmethod
    def _coverage_payload(percent: float) -> dict[str, object]:
        return {
            "files": {"fixture.py": {"summary": {}}},
            "meta": {
                "branch_coverage": True,
                "format": 3,
                "show_contexts": False,
                "timestamp": "2026-08-31T00:00:00",
                "version": "7.0.0",
            },
            "totals": {
                "covered_lines": 1,
                "missing_lines": 0,
                "num_statements": 1,
                "percent_covered": percent,
            },
        }

    @staticmethod
    def _tests_payload() -> dict[str, object]:
        return {
            "collectors": [{"nodeid": "fixture", "outcome": "passed", "result": []}],
            "created": 0.0,
            "duration": 0.1,
            "environment": {},
            "exitcode": 0,
            "root": "/tmp/fixture",
            "summary": {"collected": 1, "deselected": 0, "passed": 1, "total": 1},
            "tests": [{"nodeid": "fixture", "outcome": "passed"}],
        }

    @staticmethod
    def _profile_producer():
        script = (
            "import cProfile, os; from pathlib import Path; "
            "path = Path(os.environ['QUALITY_OUTPUT_DIR'], 'tests.pstats'); "
            "profiler = cProfile.Profile(); profiler.runcall(sum, range(10)); "
            "profiler.dump_stats(str(path))"
        )
        return quality_bundle.ProducerSpec(
            name="profiling",
            commands=(
                quality_bundle.CommandSpec(
                    argv=(sys.executable, "-c", script),
                    tool="python",
                    version_argv=(sys.executable, "--version"),
                ),
            ),
            artifacts=(quality_bundle.ArtifactSpec("profile", "tests.pstats", "pstats"),),
        )


if __name__ == "__main__":
    unittest.main()
