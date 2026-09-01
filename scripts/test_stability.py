#!/usr/bin/env python3
"""Run the test suite repeatedly and reject inconsistent outcomes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_COMMAND = (
    "pytest",
    "tests",
    "-q",
    "-m",
    "not installer_crash_matrix",
    "-p",
    "no:cacheprovider",
    "--json-report",
)


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def outcomes(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("report_invalid") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("tests"), list):
        raise ValueError("report_invalid")
    tests = payload["tests"]
    if not tests:
        raise ValueError("report_empty")
    result: dict[str, str] = {}
    for test in tests:
        if not isinstance(test, dict):
            raise ValueError("report_invalid")
        nodeid = test.get("nodeid")
        outcome = test.get("outcome")
        if not isinstance(nodeid, str) or not nodeid or not isinstance(outcome, str) or not outcome:
            raise ValueError("report_invalid")
        if nodeid in result:
            raise ValueError("report_duplicate_test")
        result[nodeid] = outcome
    return result


def _read_outcomes(path: Path) -> tuple[dict[str, str], str | None]:
    if not path.is_file():
        return {}, "report_missing"
    try:
        return outcomes(path), None
    except ValueError as error:
        return {}, str(error)


def _write_payload(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.runs <= 10:
        parser.error("--runs must be between 2 and 10")
    executions: list[dict[str, object]] = []
    baseline: dict[str, str] | None = None
    unstable: set[str] = set()
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    source_path = str(ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else source_path
    )
    with tempfile.TemporaryDirectory() as temporary:
        for run_number in range(1, args.runs + 1):
            report = Path(temporary) / f"run-{run_number}.json"
            started = time.monotonic()
            command = [
                sys.executable,
                "-B",
                "-m",
                *TEST_COMMAND,
                f"--json-report-file={report}",
            ]
            error_code: str | None = None
            try:
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    check=False,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                result = subprocess.CompletedProcess(command, 124)
                error_code = "test_timeout"
            except OSError:
                result = subprocess.CompletedProcess(command, 127)
                error_code = "test_runner_unavailable"
            current, report_error = _read_outcomes(report)
            if error_code is None:
                error_code = report_error
            if result.returncode != 0 and error_code is None:
                error_code = "test_failed"
            if baseline is None and report_error is None:
                baseline = current
            elif baseline is not None and report_error is None:
                unstable.update(
                    name
                    for name in baseline.keys() | current.keys()
                    if baseline.get(name) != current.get(name)
                )
            elif baseline is not None:
                unstable.update(baseline)
            executions.append(
                {
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error_code": error_code,
                    "exit_code": result.returncode,
                    "run": run_number,
                    "tests": len(current),
                }
            )
    failed = bool(unstable) or any(
        item["exit_code"] != 0 or item["error_code"] is not None for item in executions
    )
    payload = {
        "executions": executions,
        "runs": args.runs,
        "status": "failed" if failed else "passed",
        "unstable": sorted(unstable),
    }
    _write_payload(args.output, payload)
    if failed:
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(f"test stability: {args.runs} consistent runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
