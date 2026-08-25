#!/usr/bin/env python3
"""Measure the reproducible npm package build."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    result = subprocess.run(
        ["npm", "pack", "--dry-run", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    duration = round(time.monotonic() - started, 3)
    payload: dict[str, object] = {
        "command": "npm pack --dry-run --json",
        "duration_seconds": duration,
        "exit_code": result.returncode,
    }
    if result.returncode == 0:
        packages = json.loads(result.stdout)
        package = None
        if isinstance(packages, list) and packages:
            package = packages[0]
        elif isinstance(packages, dict) and packages:
            package = next(iter(packages.values()))
        if isinstance(package, dict):
            payload.update(
                {
                    "entry_count": len(package.get("files", [])),
                    "package_size_bytes": package.get("size"),
                    "unpacked_size_bytes": package.get("unpackedSize"),
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"package build: {duration:.3f}s")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
