from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/quality_summary.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("quality_summary_regression", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


quality_summary = _load_script()


class QualitySummarySecurityTests(unittest.TestCase):
    def test_missing_security_artifacts_are_unavailable_not_zero(self) -> None:
        status, summary = self._run_summary({})

        self.assertEqual(status, 1)
        self.assertIn("Security: **unavailable**", summary)
        self.assertNotIn("0 medium/high Bandit findings", summary)
        self.assertNotIn("0 dependency vulnerabilities", summary)

    def test_failed_security_artifacts_are_not_reported_as_zero(self) -> None:
        status, summary = self._run_summary(
            {
                "bandit.json": {"exit_code": 1},
                "pip-audit.json": {"exit_code": 2},
            }
        )

        self.assertEqual(status, 1)
        self.assertIn("Security: **FAILED**", summary)
        self.assertNotIn("0 medium/high Bandit findings", summary)
        self.assertNotIn("0 dependency vulnerabilities", summary)

    def test_unproven_empty_security_payloads_are_unavailable(self) -> None:
        status, summary = self._run_summary(
            {
                "bandit.json": {"results": []},
                "pip-audit.json": {"dependencies": []},
            }
        )

        self.assertEqual(status, 1)
        self.assertIn("Security: **unavailable**", summary)
        self.assertNotIn("0 medium/high Bandit findings", summary)
        self.assertNotIn("0 dependency vulnerabilities", summary)

    def test_complete_zero_finding_security_payloads_pass(self) -> None:
        status, summary = self._run_summary(
            {
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
            }
        )

        self.assertEqual(status, 0, summary)
        self.assertIn("0 medium/high Bandit findings", summary)
        self.assertIn("0 dependency vulnerabilities", summary)

    def test_ci_requires_security_status_artifact(self) -> None:
        artifacts = {
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
        }
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False):
            status, summary = self._run_summary(artifacts)

        self.assertEqual(status, 1)
        self.assertIn("Security: **unavailable**", summary)

    def test_failed_security_status_artifact_is_explicit(self) -> None:
        status, summary = self._run_summary(
            {
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
                "security-status.json": {"exit_code": 1},
            }
        )

        self.assertEqual(status, 1)
        self.assertIn("Security: **FAILED**", summary)
        self.assertIn("security-status.json", summary)
        self.assertNotIn("0 medium/high Bandit findings", summary)

    def _run_summary(self, security: dict[str, dict[str, object]]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as temporary:
            quality_root = Path(temporary) / "quality"
            quality_root.mkdir()
            self._write_required_quality_inputs(quality_root)
            for name, payload in security.items():
                (quality_root / name).write_text(json.dumps(payload), encoding="utf-8")
            output = Path(temporary) / "summary.md"
            with (
                patch.object(quality_summary, "QUALITY_ROOT", quality_root),
                patch.object(sys, "argv", ["quality_summary.py", "--output", str(output)]),
            ):
                status = quality_summary.main()
            return status, output.read_text(encoding="utf-8")

    @staticmethod
    def _write_required_quality_inputs(quality_root: Path) -> None:
        (quality_root / "coverage.json").write_text(
            json.dumps({"totals": {"percent_covered": 90.0}}),
            encoding="utf-8",
        )
        (quality_root / "stability.json").write_text(
            json.dumps(
                {
                    "runs": 2,
                    "unstable": [],
                    "executions": [{"exit_code": 0}, {"exit_code": 0}],
                }
            ),
            encoding="utf-8",
        )
        (quality_root / "build.json").write_text(
            json.dumps(
                {
                    "entry_count": 1,
                    "package_size_bytes": 1,
                    "unpacked_size_bytes": 1,
                    "duration_seconds": 0.1,
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
