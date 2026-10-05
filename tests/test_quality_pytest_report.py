from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_quality_bundle import quality_bundle


class QualityPytestReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            test = root / "test_report.py"
            test.write_text(
                "import unittest\n"
                "class ReportCase(unittest.TestCase):\n"
                "    def test_subtests(self):\n"
                "        for value in range(2):\n"
                "            with self.subTest(value=value):\n"
                "                self.assertGreaterEqual(value, 0)\n",
                encoding="utf-8",
            )
            report = root / "report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    str(test),
                    "--json-report",
                    f"--json-report-file={report}",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if completed.returncode != 0:
                raise AssertionError(completed.stdout + completed.stderr)
            cls.report = json.loads(report.read_text(encoding="utf-8"))

    def test_actual_installed_pytest_subtest_report_is_verified(self) -> None:
        self.assertEqual(self.report["tests"][0]["outcome"], "subtests passed")
        self.assertNotIn("passed", self.report["summary"])
        self.assertNotIn("deselected", self.report["summary"])
        for producer in ("test", "coverage"):
            with self.subTest(producer=producer):
                quality_bundle._validate_artifact_payload(
                    producer, "tests", self.report, expected_exit_code=0
                )

    def test_failed_skipped_unknown_and_contradictory_reports_are_not_verified(self) -> None:
        mutations = {
            "failed": lambda report: report["tests"][0].update(outcome="failed"),
            "skipped": lambda report: report["tests"][0].update(outcome="skipped"),
            "subtest_failed": lambda report: report["tests"][0].update(outcome="subtests failed"),
            "subtest_skipped": lambda report: report["tests"][0].update(outcome="subtests skipped"),
            "unknown": lambda report: report["tests"][0].update(outcome="success"),
            "exit_failure": lambda report: report.update(exitcode=1),
            "hidden_failure": lambda report: report["summary"].update(failed=1),
            "hidden_skip": lambda report: report["summary"].update(skipped=1),
            "count_mismatch": lambda report: report["summary"].update(passed=1),
            "missing_count": lambda report: report["summary"].pop("subtests passed"),
            "missing_tests": lambda report: report.update(tests=[]),
            "extra_test": lambda report: report["tests"].append(report["tests"][0]),
            "wrong_total": lambda report: report["summary"].update(total=2),
            "wrong_collected": lambda report: report["summary"].update(collected=2),
            "negative_deselected": lambda report: report["summary"].update(deselected=-1),
            "boolean_count": lambda report: report["summary"].update({"subtests passed": True}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                report = copy.deepcopy(self.report)
                mutate(report)
                with self.assertRaisesRegex(
                    quality_bundle.QualityBundleError, "quality_artifact_invalid"
                ):
                    quality_bundle._validate_artifact_payload(
                        "test", "tests", report, expected_exit_code=report["exitcode"]
                    )

    def test_deselected_tests_are_accounted_for_without_claiming_they_ran(self) -> None:
        report = copy.deepcopy(self.report)
        report["summary"].update(deselected=2, collected=3)
        quality_bundle._validate_artifact_payload("test", "tests", report, expected_exit_code=0)
