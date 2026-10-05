#!/usr/bin/env python3
"""Measure the reproducible npm package build."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ["npm", "pack", "--dry-run", "--json"]
COMMAND_TEXT = " ".join(COMMAND)
PACKAGE_COMMANDS = {
    "herdr-orchestrator": COMMAND,
    "herdr-manager": [*COMMAND, "./packages/herdr-manager"],
}


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _package_from_output(stdout: str) -> dict[str, object]:
    try:
        payload = json.loads(stdout, object_pairs_hook=_object_without_duplicates)
    except (TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("npm_pack_response_invalid") from error

    package: object
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ValueError("npm_pack_response_invalid")
        package = payload[0]
    elif isinstance(payload, dict) and {"files", "size", "unpackedSize"}.issubset(payload):
        package = payload
    elif isinstance(payload, dict) and len(payload) == 1:
        package = next(iter(payload.values()))
    else:
        raise ValueError("npm_pack_response_invalid")

    if not isinstance(package, dict):
        raise ValueError("npm_pack_response_invalid")
    if not isinstance(package.get("files"), list):
        raise ValueError("npm_pack_response_invalid")
    if not _nonnegative_integer(package.get("size")):
        raise ValueError("npm_pack_response_invalid")
    if not _nonnegative_integer(package.get("unpackedSize")):
        raise ValueError("npm_pack_response_invalid")
    return package


def _write_payload(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    packages: dict[str, dict[str, object]] = {}
    exit_code = 0
    return_code = 0
    error_code: str | None = None
    for name, command in PACKAGE_COMMANDS.items():
        package_error: str | None = None
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess(command, 124, "", "")
            package_error = "npm_pack_timeout"
        except OSError:
            result = subprocess.CompletedProcess(command, 127, "", "")
            package_error = "npm_pack_unavailable"
        package_payload: dict[str, object] = {
            "command": " ".join(command),
            "exit_code": result.returncode,
        }
        if package_error is None and result.returncode == 0:
            try:
                package = _package_from_output(result.stdout)
            except ValueError as error:
                package_error = str(error)
            else:
                package_payload.update(
                    {
                        "entry_count": len(package["files"]),
                        "package_size_bytes": package["size"],
                        "unpacked_size_bytes": package["unpackedSize"],
                    }
                )
        if result.returncode != 0 and package_error is None:
            package_error = "npm_pack_failed"
        if package_error is not None:
            package_payload.update({"error_code": package_error, "status": "failed"})
            if result.returncode != 0 and exit_code == 0:
                exit_code = result.returncode
            if return_code == 0:
                return_code = result.returncode or 1
            if error_code is None:
                error_code = package_error
        else:
            package_payload["status"] = "passed"
        packages[name] = package_payload
    duration = round(time.monotonic() - started, 3)
    payload: dict[str, object] = {
        "command": COMMAND_TEXT,
        "duration_seconds": duration,
        "exit_code": exit_code,
        "packages": packages,
    }
    completed = [item for item in packages.values() if item.get("status") == "passed"]
    payload.update(
        {
            "entry_count": sum(int(item["entry_count"]) for item in completed),
            "package_size_bytes": sum(int(item["package_size_bytes"]) for item in completed),
            "unpacked_size_bytes": sum(int(item["unpacked_size_bytes"]) for item in completed),
        }
    )
    if error_code is not None:
        payload.update({"error_code": error_code, "status": "failed"})
    else:
        payload["status"] = "passed"
    _write_payload(args.output, payload)
    if error_code is not None:
        print(json.dumps(payload, sort_keys=True))
        return return_code
    print(f"package build: {duration:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
