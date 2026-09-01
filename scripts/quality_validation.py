"""Validation contracts for published quality evidence."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from math import isfinite
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    """A stable quality evidence validation failure."""


def _invalid() -> None:
    raise ValidationError("quality_artifact_invalid")


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite_nonnegative(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return isfinite(value) and value >= 0
    except (OverflowError, ValueError):
        return False


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _mapping(payload: dict[str, object], key: str) -> dict[str, object] | None:
    value = payload.get(key)
    return value if isinstance(value, dict) else None


def _list(payload: dict[str, object], key: str) -> list[object] | None:
    value = payload.get(key)
    return value if isinstance(value, list) else None


def input_digest(inputs: Sequence[str]) -> str:
    import hashlib
    import os

    digest = hashlib.sha256()
    for name in inputs:
        digest.update(os.fsencode(name))
        digest.update(b"\0")
    return digest.hexdigest()


def path_has_symlink(root: Path, relative: Path) -> bool:
    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def validate_command_contract(
    payload: object,
    expected: Any,
    tracked_files: Callable[[], Sequence[str]],
    empty_input_sha256: str,
) -> None:
    if not isinstance(payload, dict):
        raise ValidationError("quality_command_mismatch")
    argv = payload.get("argv")
    input_count = payload.get("input_count")
    input_sha256 = payload.get("input_sha256")
    if tuple(argv) != tuple(expected.argv) or payload.get("tool") != expected.tool:
        raise ValidationError("quality_command_mismatch")
    if expected.include_tracked_files:
        if "--" not in expected.argv:
            raise ValidationError("quality_command_mismatch")
        inputs = tuple(tracked_files())
        if (
            payload.get("argv_count") != len(expected.argv) + input_count
            or input_count != len(inputs)
            or input_sha256 != input_digest(inputs)
        ):
            raise ValidationError("quality_command_mismatch")
    elif (
        payload.get("argv_count") != len(expected.argv)
        or input_count != 0
        or input_sha256 != empty_input_sha256
    ):
        raise ValidationError("quality_command_mismatch")


def _validate_coverage(payload: dict[str, object], key: str) -> None:
    if key == "coverage":
        meta = _mapping(payload, "meta")
        files = _mapping(payload, "files")
        totals = _mapping(payload, "totals")
        if (
            meta is None
            or files is None
            or totals is None
            or not _nonnegative_integer(meta.get("format"))
            or not _text(meta.get("version"))
            or not _text(meta.get("timestamp"))
            or not isinstance(meta.get("branch_coverage"), bool)
            or not isinstance(meta.get("show_contexts"), bool)
            or not totals
        ):
            _invalid()
        for name, value in totals.items():
            if not _finite_nonnegative(value) or ("percent" in name and value > 100):
                _invalid()
        for file_payload in files.values():
            if not isinstance(file_payload, dict) or not isinstance(
                file_payload.get("summary"), dict
            ):
                _invalid()
        return

    tests = _list(payload, "tests")
    collectors = _list(payload, "collectors")
    summary = _mapping(payload, "summary")
    if (
        tests is None
        or not tests
        or collectors is None
        or summary is None
        or not _finite_nonnegative(payload.get("created"))
        or not _finite_nonnegative(payload.get("duration"))
        or not isinstance(payload.get("exitcode"), int)
        or isinstance(payload.get("exitcode"), bool)
        or not _text(payload.get("root"))
        or not isinstance(payload.get("environment"), dict)
    ):
        _invalid()
    counts = tuple(summary.get(name) for name in ("total", "passed", "collected", "deselected"))
    if not all(_nonnegative_integer(value) for value in counts):
        _invalid()
    total, passed, collected, deselected = counts
    assert isinstance(total, int)
    assert isinstance(passed, int)
    assert isinstance(collected, int)
    assert isinstance(deselected, int)
    if total == 0 or passed > total or collected < total or deselected > collected:
        _invalid()
    for test in tests:
        if (
            not isinstance(test, dict)
            or not _text(test.get("nodeid"))
            or not _text(test.get("outcome"))
        ):
            _invalid()


def _validate_stability(payload: dict[str, object]) -> None:
    runs = payload.get("runs")
    executions = _list(payload, "executions")
    unstable = _list(payload, "unstable")
    if (
        not isinstance(runs, int)
        or isinstance(runs, bool)
        or not 2 <= runs <= 10
        or executions is None
        or len(executions) != runs
        or unstable is None
        or not all(_text(name) for name in unstable)
        or payload.get("status") not in {"passed", "failed"}
    ):
        _invalid()
    for index, execution in enumerate(executions, start=1):
        if not isinstance(execution, dict) or execution.get("run") != index:
            _invalid()
        if (
            not _nonnegative_integer(execution.get("tests"))
            or not _finite_nonnegative(execution.get("duration_seconds"))
            or not isinstance(execution.get("exit_code"), int)
            or isinstance(execution.get("exit_code"), bool)
            or (execution.get("error_code") is not None and not _text(execution.get("error_code")))
        ):
            _invalid()
    if payload.get("status") == "passed" and (
        unstable or any(execution.get("exit_code") != 0 for execution in executions)
    ):
        _invalid()


def _validate_bandit(payload: dict[str, object]) -> None:
    errors = _list(payload, "errors")
    results = _list(payload, "results")
    metrics = _mapping(payload, "metrics")
    if (
        errors is None
        or results is None
        or metrics is None
        or not _text(payload.get("generated_at"))
        or not all(isinstance(item, dict) for item in errors)
        or not all(isinstance(item, dict) for item in results)
    ):
        _invalid()


def _validate_pip_audit(payload: dict[str, object]) -> None:
    dependencies = _list(payload, "dependencies")
    fixes = _list(payload, "fixes")
    if dependencies is None or not dependencies or fixes is None:
        _invalid()
    for dependency in dependencies:
        if (
            not isinstance(dependency, dict)
            or not _text(dependency.get("name"))
            or not _text(dependency.get("version"))
            or not isinstance(dependency.get("vulns"), list)
            or not all(isinstance(vulnerability, dict) for vulnerability in dependency["vulns"])
        ):
            _invalid()


def _validate_npm_audit(payload: dict[str, object]) -> None:
    metadata = _mapping(payload, "metadata")
    vulnerabilities = payload.get("vulnerabilities")
    if (
        not isinstance(payload.get("auditReportVersion"), int)
        or isinstance(payload.get("auditReportVersion"), bool)
        or payload.get("auditReportVersion") < 1
        or metadata is None
        or not isinstance(vulnerabilities, dict)
        or not isinstance(metadata.get("vulnerabilities"), dict)
        or not isinstance(metadata.get("dependencies"), dict)
    ):
        _invalid()
    for group in (metadata["vulnerabilities"], metadata["dependencies"]):
        assert isinstance(group, dict)
        if not all(_nonnegative_integer(value) for value in group.values()):
            _invalid()


def _validate_build(payload: dict[str, object]) -> None:
    if (
        not _text(payload.get("command"))
        or payload.get("status") not in {"passed", "failed"}
        or not isinstance(payload.get("exit_code"), int)
        or isinstance(payload.get("exit_code"), bool)
        or not _finite_nonnegative(payload.get("duration_seconds"))
        or not _nonnegative_integer(payload.get("entry_count"))
        or not _nonnegative_integer(payload.get("package_size_bytes"))
        or not _nonnegative_integer(payload.get("unpacked_size_bytes"))
    ):
        _invalid()


def validate_artifact_payload(producer: str, key: str, payload: dict[str, object]) -> None:
    if producer == "coverage" and key in {"coverage", "tests"}:
        _validate_coverage(payload, key)
    elif producer == "stability" and key == "stability":
        _validate_stability(payload)
    elif producer == "security":
        if key == "bandit":
            _validate_bandit(payload)
        elif key == "pip-audit":
            _validate_pip_audit(payload)
        elif key in {"npm-audit-root", "npm-audit-manager"}:
            _validate_npm_audit(payload)
    elif producer == "build" and key == "build":
        _validate_build(payload)


def validate_bundle_inventory(bundle: Path, expected_files: set[Path]) -> None:
    actual_files: set[Path] = set()
    actual_dirs: set[Path] = set()
    try:
        for candidate in bundle.rglob("*"):
            relative = candidate.relative_to(bundle)
            if candidate.is_symlink():
                raise ValidationError("quality_bundle_inventory_invalid")
            if candidate.is_file():
                actual_files.add(relative)
            elif candidate.is_dir():
                actual_dirs.add(relative)
            else:
                raise ValidationError("quality_bundle_inventory_invalid")
    except OSError as error:
        raise ValidationError("quality_bundle_inventory_invalid") from error
    if actual_files != expected_files:
        raise ValidationError("quality_bundle_inventory_invalid")
    expected_dirs: set[Path] = set()
    for relative in expected_files:
        parent = relative.parent
        while parent != Path("."):
            expected_dirs.add(parent)
            parent = parent.parent
    if actual_dirs != expected_dirs:
        raise ValidationError("quality_bundle_inventory_invalid")
