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


def command_runs(argv: Sequence[str]) -> int | None:
    for index, value in enumerate(argv[:-1]):
        if value == "--runs":
            try:
                runs = int(argv[index + 1])
            except (TypeError, ValueError):
                return None
            return runs if runs >= 2 else None
    return None


def expected_runs(commands: Sequence[object]) -> int | None:
    if not commands:
        return None
    first = commands[0]
    argv = getattr(first, "argv", ())
    return command_runs(argv) if isinstance(argv, tuple) else None


def expected_branch_coverage(commands: Sequence[object]) -> bool:
    return any("--cov-branch" in getattr(command, "argv", ()) for command in commands)


def path_has_symlink(root: Path, relative: Path) -> bool:
    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


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


def validate_artifact_payload(
    producer: str,
    key: str,
    payload: dict[str, object],
    *,
    expected_runs: int | None = None,
    expected_exit_code: int | None = None,
    expected_branch: bool = False,
) -> None:
    """Validate known producer output before treating it as verified evidence."""
    valid = True
    if producer == "coverage" and key == "coverage":
        meta = payload.get("meta")
        files = payload.get("files")
        totals = payload.get("totals")
        percent = totals.get("percent_covered") if isinstance(totals, dict) else None
        valid = (
            isinstance(meta, dict)
            and _nonnegative_integer(meta.get("format"))
            and meta.get("format", 0) > 0
            and _nonempty_text(meta.get("version"))
            and _nonempty_text(meta.get("timestamp"))
            and isinstance(meta.get("branch_coverage"), bool)
            and isinstance(meta.get("show_contexts"), bool)
            and (not expected_branch or meta.get("branch_coverage") is True)
            and isinstance(files, dict)
            and bool(files)
            and isinstance(totals, dict)
            and bool(totals)
            and _finite_number(percent)
            and isinstance(percent, (int, float))
            and 0 <= percent <= 100
        )
        if valid:
            for name, value in totals.items():
                if not isinstance(name, str):
                    valid = False
                    break
                if name.endswith("_display"):
                    try:
                        display = float(value)
                    except (TypeError, ValueError):
                        valid = False
                        break
                    numeric_name = name.removesuffix("_display")
                    numeric = totals.get(numeric_name)
                    if (
                        not _nonempty_text(value)
                        or not isfinite(display)
                        or not _finite_number(numeric)
                        or str(round(float(numeric))) != value
                    ):
                        valid = False
                        break
                elif not _finite_number(value) or value < 0 or ("percent" in name and value > 100):
                    valid = False
                    break
            if valid:
                valid = all(
                    isinstance(file_payload, dict) and isinstance(file_payload.get("summary"), dict)
                    for file_payload in files.values()
                )
    elif key == "tests" and producer in {"coverage", "test"}:
        tests = payload.get("tests")
        summary = payload.get("summary")
        valid = (
            isinstance(tests, list)
            and bool(tests)
            and _finite_number(payload.get("created"))
            and _finite_number(payload.get("duration"))
            and payload.get("created") >= 0
            and payload.get("duration") >= 0
            and isinstance(payload.get("exitcode"), int)
            and not isinstance(payload.get("exitcode"), bool)
            and (expected_exit_code is None or payload.get("exitcode") == expected_exit_code)
            and _nonempty_text(payload.get("root"))
            and isinstance(payload.get("environment"), dict)
            and isinstance(payload.get("collectors"), list)
            and isinstance(summary, dict)
            and all(
                _nonnegative_integer(summary.get(name))
                for name in ("total", "passed", "collected", "deselected")
            )
            and all(
                isinstance(test, dict)
                and _nonempty_text(test.get("nodeid"))
                and _nonempty_text(test.get("outcome"))
                for test in tests
            )
        )
        if valid:
            total = summary["total"]
            passed = summary["passed"]
            collected = summary["collected"]
            deselected = summary["deselected"]
            valid = (
                isinstance(total, int)
                and isinstance(passed, int)
                and isinstance(collected, int)
                and isinstance(deselected, int)
                and total > 0
                and passed <= total
                and collected >= total
                and deselected <= collected
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
            and isinstance(payload.get("status"), str)
            and payload.get("status") in {"passed", "failed"}
            and (expected_runs is None or runs == expected_runs)
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
        if valid:
            failed_execution = any(
                item["exit_code"] != 0 or item.get("error_code") is not None for item in executions
            ) or bool(unstable)
            valid = (payload["status"] == "failed") == failed_execution
            if expected_exit_code is not None:
                valid = valid and ((expected_exit_code == 0) == (payload["status"] == "passed"))
    elif producer == "security" and key == "bandit":
        metrics = payload.get("metrics")
        totals = metrics.get("_totals") if isinstance(metrics, dict) else None
        valid = (
            isinstance(payload.get("errors"), list)
            and isinstance(payload.get("results"), list)
            and isinstance(metrics, dict)
            and isinstance(totals, dict)
            and bool(totals)
            and _nonempty_text(payload.get("generated_at"))
            and all(isinstance(item, dict) for item in payload["errors"])
            and all(isinstance(item, dict) for item in payload["results"])
        )
        if valid:
            valid = all(_finite_number(value) and value >= 0 for value in totals.values())
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
            and isinstance(metadata.get("dependencies"), dict)
            and isinstance(payload.get("vulnerabilities"), dict)
        )
        if valid:
            vulnerability_counts = metadata["vulnerabilities"]
            dependency_counts = metadata["dependencies"]
            valid = (
                all(_nonnegative_integer(value) for value in vulnerability_counts.values())
                and all(_nonnegative_integer(value) for value in dependency_counts.values())
                and _npm_counts_are_consistent(vulnerability_counts, payload["vulnerabilities"])
            )
    elif producer == "build" and key == "build":
        valid = (
            _nonempty_text(payload.get("command"))
            and isinstance(payload.get("status"), str)
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
        if valid and expected_exit_code is not None:
            valid = payload["exit_code"] == expected_exit_code
        if valid:
            valid = (payload["status"] == "passed") == (payload["exit_code"] == 0)
    if not valid:
        raise QualityBundleError("quality_artifact_invalid")


def _npm_counts_are_consistent(counts: dict[str, object], vulnerabilities: object) -> bool:
    severity_names = ("info", "low", "moderate", "high", "critical")
    if not all(name in counts for name in severity_names + ("total",)):
        return False
    total = counts["total"]
    if not _nonnegative_integer(total) or not isinstance(vulnerabilities, dict):
        return False
    severity_total = sum(counts[name] for name in severity_names)
    return severity_total == total and ((total == 0) == (not vulnerabilities))


def finding_count(key: str, payload: dict[str, object]) -> int:
    if key == "bandit":
        return len(payload["results"]) + len(payload["errors"])
    if key == "pip-audit":
        return sum(len(item["vulns"]) for item in payload["dependencies"])
    if key in {"npm-audit-root", "npm-audit-manager"}:
        metadata = payload["metadata"]
        return int(metadata["vulnerabilities"]["total"])
    return 0


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
