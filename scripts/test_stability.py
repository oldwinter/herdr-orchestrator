#!/usr/bin/env python3
"""Run the test suite repeatedly and reject inconsistent outcomes."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def outcomes(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(test["nodeid"]): str(test["outcome"]) for test in payload["tests"]}


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
    with tempfile.TemporaryDirectory() as temporary:
        for run_number in range(1, args.runs + 1):
            report = Path(temporary) / f"run-{run_number}.json"
            started = time.monotonic()
            result = subprocess.run(
                [
                    "python",
                    "-m",
                    "pytest",
                    "tests",
                    "-q",
                    "--json-report",
                    f"--json-report-file={report}",
                ],
                cwd=ROOT,
                check=False,
            )
            current = outcomes(report) if report.is_file() else {}
            if baseline is None:
                baseline = current
            else:
                unstable.update(
                    name
                    for name in baseline.keys() | current.keys()
                    if baseline.get(name) != current.get(name)
                )
            executions.append(
                {
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "exit_code": result.returncode,
                    "run": run_number,
                    "tests": len(current),
                }
            )
    payload = {
        "executions": executions,
        "runs": args.runs,
        "unstable": sorted(unstable),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if unstable or any(item["exit_code"] != 0 for item in executions):
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(f"test stability: {args.runs} consistent runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
