"""Validation helpers shared by the quality bundle runner."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from math import isfinite
from pathlib import Path


class QualityBundleError(ValueError):
    """A stable quality-bundle contract failure."""


EMPTY_INPUT_SHA256 = hashlib.sha256(b"").hexdigest()
SOURCE_PROBE_FAILURE_SHA256 = hashlib.sha256(b"quality_source_probe_unavailable").hexdigest()


def inventory_digest(inputs: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for name in inputs:
        digest.update(os.fsencode(name))
        digest.update(b"\0")
    return digest.hexdigest()


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return isfinite(value)
    except (OverflowError, ValueError):
        return False


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def validate_artifact_payload(producer: str, key: str, payload: dict[str, object]) -> None:
    """Validate known producer output before treating it as verified evidence."""
    valid = True
    if producer == "coverage" and key == "coverage":
        totals = payload.get("totals")
        percent = totals.get("percent_covered") if isinstance(totals, dict) else None
        valid = (
            isinstance(totals, dict)
            and bool(totals)
            and _finite_number(percent)
            and isinstance(percent, (int, float))
            and 0 <= percent <= 100
        )
    elif key == "tests" and producer in {"coverage", "test"}:
        tests = payload.get("tests")
        valid = (
            isinstance(tests, list)
            and bool(tests)
            and all(
                isinstance(test, dict)
                and _nonempty_text(test.get("nodeid"))
                and _nonempty_text(test.get("outcome"))
                for test in tests
            )
        )
    elif producer == "stability" and key == "stability":
        runs = payload.get("runs")
        executions = payload.get("executions")
        unstable = payload.get("unstable")
        valid = (
            isinstance(runs, int)
            and not isinstance(runs, bool)
            and 2 <= runs <= 10
            and isinstance(executions, list)
            and len(executions) == runs
            and isinstance(unstable, list)
            and all(_nonempty_text(name) for name in unstable)
            and payload.get("status") in {"passed", "failed"}
            and all(
                isinstance(item, dict)
                and item.get("run") == index
                and _nonnegative_integer(item.get("tests"))
                and _finite_number(item.get("duration_seconds"))
                and isinstance(item.get("exit_code"), int)
                and not isinstance(item.get("exit_code"), bool)
                for index, item in enumerate(executions, 1)
            )
        )
    elif producer == "security" and key == "bandit":
        valid = (
            isinstance(payload.get("errors"), list)
            and isinstance(payload.get("results"), list)
            and isinstance(payload.get("metrics"), dict)
            and _nonempty_text(payload.get("generated_at"))
            and all(isinstance(item, dict) for item in payload["errors"])
            and all(isinstance(item, dict) for item in payload["results"])
        )
    elif producer == "security" and key == "pip-audit":
        dependencies = payload.get("dependencies")
        valid = (
            isinstance(dependencies, list)
            and bool(dependencies)
            and isinstance(payload.get("fixes"), list)
        )
        if valid:
            valid = all(
                isinstance(item, dict)
                and _nonempty_text(item.get("name"))
                and _nonempty_text(item.get("version"))
                and isinstance(item.get("vulns"), list)
                and all(isinstance(vulnerability, dict) for vulnerability in item["vulns"])
                for item in dependencies
            )
    elif producer == "security" and key in {"npm-audit-root", "npm-audit-manager"}:
        metadata = payload.get("metadata")
        valid = (
            _nonnegative_integer(payload.get("auditReportVersion"))
            and payload.get("auditReportVersion") > 0
            and isinstance(metadata, dict)
            and isinstance(metadata.get("vulnerabilities"), dict)
            and isinstance(payload.get("vulnerabilities"), dict)
        )
    elif producer == "build" and key == "build":
        valid = (
            _nonempty_text(payload.get("command"))
            and payload.get("status") in {"passed", "failed"}
            and isinstance(payload.get("exit_code"), int)
            and not isinstance(payload.get("exit_code"), bool)
            and _finite_number(payload.get("duration_seconds"))
            and payload.get("duration_seconds") >= 0
            and all(
                _nonnegative_integer(payload.get(name))
                for name in ("entry_count", "package_size_bytes", "unpacked_size_bytes")
            )
        )
    if not valid:
        raise QualityBundleError("quality_artifact_invalid")


def validate_bundle_inventory(bundle: Path, expected_files: set[Path]) -> None:
    actual_files: set[Path] = set()
    actual_dirs: set[Path] = set()

    def visit(directory: Path) -> None:
        try:
            entries = os.scandir(directory)
        except OSError as error:
            raise QualityBundleError("quality_bundle_inventory_invalid") from error
        with entries:
            for entry in entries:
                relative = Path(entry.path).relative_to(bundle)
                try:
                    if entry.is_symlink():
                        raise QualityBundleError("quality_bundle_inventory_invalid")
                    if entry.is_dir(follow_symlinks=False):
                        actual_dirs.add(relative)
                        visit(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        actual_files.add(relative)
                    else:
                        raise QualityBundleError("quality_bundle_inventory_invalid")
                except OSError as error:
                    raise QualityBundleError("quality_bundle_inventory_invalid") from error

    visit(bundle)
    expected_dirs = {
        parent for path in expected_files for parent in path.parents if parent != Path(".")
    }
    if actual_files != expected_files or actual_dirs != expected_dirs:
        raise QualityBundleError("quality_bundle_inventory_invalid")
