#!/usr/bin/env python3
"""Summarize machine-readable quality results for humans and PR automation."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUALITY_ROOT = ROOT / ".orchestrator" / "quality"
COVERAGE_THRESHOLD = 80.0
SECURITY_STATUS_ARTIFACT = "security-status.json"


@dataclass(frozen=True)
class Artifact:
    payload: dict[str, object] | None
    error: str | None = None


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _load_artifact(name: str) -> Artifact:
    path = QUALITY_ROOT / name
    if not path.is_file():
        return Artifact(None, f"{name} missing")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError):
        return Artifact(None, f"{name} invalid_json")
    if not isinstance(payload, dict):
        return Artifact(None, f"{name} invalid_shape")
    return Artifact(payload)


def load(name: str) -> dict[str, object]:
    """Load an artifact while retaining the original helper's empty fallback."""
    artifact = _load_artifact(name)
    return artifact.payload or {}


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return isfinite(value)
    except (OverflowError, ValueError):
        return False


def _command_failure(artifact: Artifact) -> tuple[str, str] | None:
    if artifact.error is not None:
        return "unavailable", artifact.error
    assert artifact.payload is not None
    exit_code = artifact.payload.get("exit_code")
    if exit_code is not None and not _integer(exit_code):
        return "unavailable", "invalid exit_code"
    if isinstance(exit_code, int) and exit_code != 0:
        return "failed", f"exit code {exit_code}"
    error_code = artifact.payload.get("error_code")
    if error_code is not None:
        if not isinstance(error_code, str) or not error_code:
            return "unavailable", "invalid error_code"
        return "failed", f"error code {error_code}"
    status = artifact.payload.get("status")
    if status is not None and (not isinstance(status, str) or status not in {"passed", "failed"}):
        return "unavailable", "invalid status"
    if status == "failed":
        return "failed", "reported failure"
    return None


def _coverage(artifact: Artifact) -> tuple[str, str]:
    failure = _command_failure(artifact)
    if failure is not None:
        status, detail = failure
        label = status.upper() if status == "failed" else status
        return status, f"- Coverage: **{label}** ({detail})"
    assert artifact.payload is not None
    totals = artifact.payload.get("totals")
    percent = totals.get("percent_covered") if isinstance(totals, dict) else None
    if not _finite_number(percent):
        return "unavailable", "- Coverage: **unavailable** (coverage totals missing)"
    if not 0 <= percent <= 100:
        return "unavailable", "- Coverage: **unavailable** (coverage percent invalid)"
    line = f"- Coverage: **{percent}%** with an enforced 80% branch-aware threshold"
    if percent < COVERAGE_THRESHOLD:
        return "failed", f"{line} (**FAILED**; below threshold)"
    return "passed", line


def _stability(artifact: Artifact) -> tuple[str, str]:
    failure = _command_failure(artifact)
    if failure is not None and failure[0] == "unavailable":
        return failure[0], f"- Stability: **unavailable** ({failure[1]})"
    assert artifact.payload is not None
    runs = artifact.payload.get("runs")
    unstable = artifact.payload.get("unstable")
    executions = artifact.payload.get("executions")
    if (
        not _integer(runs)
        or runs < 2
        or not isinstance(unstable, list)
        or not all(isinstance(name, str) and name for name in unstable)
        or not isinstance(executions, list)
        or len(executions) != runs
    ):
        return "unavailable", "- Stability: **unavailable** (stability schema invalid)"
    exit_codes: list[int] = []
    error_codes: list[str] = []
    for execution in executions:
        if not isinstance(execution, dict):
            return "unavailable", "- Stability: **unavailable** (stability schema invalid)"
        exit_code = execution.get("exit_code")
        if not _integer(exit_code):
            return "unavailable", "- Stability: **unavailable** (stability exit code invalid)"
        if exit_code != 0:
            exit_codes.append(exit_code)
        error_code = execution.get("error_code")
        if error_code is not None:
            if not isinstance(error_code, str) or not error_code:
                return "unavailable", "- Stability: **unavailable** (stability error code invalid)"
            error_codes.append(error_code)
    line = f"- Stability: **{runs}** repeated runs, **{len(unstable)}** unstable tests"
    reasons: list[str] = []
    if exit_codes:
        reasons.append(f"exit codes: {', '.join(str(code) for code in exit_codes)}")
    if error_codes:
        reasons.append(f"error codes: {', '.join(error_codes)}")
    if unstable:
        reasons.append(f"unstable tests: {len(unstable)}")
    if failure is not None:
        reasons.append(failure[1])
    if reasons:
        return (
            "failed",
            f"- Stability: **FAILED** ({runs} repeated runs, "
            f"{len(unstable)} unstable tests; {'; '.join(reasons)})",
        )
    return "passed", line


def _build(artifact: Artifact) -> tuple[str, str]:
    failure = _command_failure(artifact)
    if failure is not None:
        status, detail = failure
        label = status.upper() if status == "failed" else status
        return status, f"- Build: **{label}** ({detail})"
    assert artifact.payload is not None
    fields = (
        artifact.payload.get("entry_count"),
        artifact.payload.get("package_size_bytes"),
        artifact.payload.get("unpacked_size_bytes"),
    )
    if not all(_integer(value) and value >= 0 for value in fields):
        return "unavailable", "- Build: **unavailable** (build metrics missing)"
    duration = artifact.payload.get("duration_seconds")
    if not _finite_number(duration):
        return "unavailable", "- Build: **unavailable** (build duration invalid)"
    return "passed", f"- Build: **{duration}s**, **{fields[1]} bytes** packed"


def _security(artifact: Artifact, label: str) -> tuple[str, int | None, str | None]:
    failure = _command_failure(artifact)
    if failure is not None:
        return failure[0], None, failure[1]
    assert artifact.payload is not None
    if label == "bandit":
        results = artifact.payload.get("results")
        if not isinstance(results, list):
            return "unavailable", None, "results missing"
        if not all(isinstance(result, dict) for result in results):
            return "unavailable", None, "results schema invalid"
        errors = artifact.payload.get("errors")
        if not isinstance(errors, list):
            return "unavailable", None, "errors missing or invalid"
        if errors:
            return "failed", len(results), "tool errors present"
        if not results and (
            not isinstance(artifact.payload.get("generated_at"), str)
            or not artifact.payload["generated_at"]
            or not isinstance(artifact.payload.get("metrics"), dict)
        ):
            return "unavailable", None, "completion metadata missing"
        return ("failed", len(results), "findings") if results else ("passed", 0, None)
    dependencies = artifact.payload.get("dependencies")
    if not isinstance(dependencies, list):
        return "unavailable", None, "dependencies missing"
    if not dependencies:
        return "unavailable", None, "dependencies empty"
    if not isinstance(artifact.payload.get("fixes"), list):
        return "unavailable", None, "fixes missing or invalid"
    count = 0
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            return "unavailable", None, "dependency schema invalid"
        if "vulns" not in dependency:
            if isinstance(dependency.get("skip_reason"), str) and dependency["skip_reason"]:
                continue
            return "unavailable", None, "dependency schema invalid"
        if not isinstance(dependency["vulns"], list) or not all(
            isinstance(vulnerability, dict) for vulnerability in dependency["vulns"]
        ):
            return "unavailable", None, "dependency schema invalid"
        count += len(dependency["vulns"])
    return ("failed", count, "vulnerabilities") if count else ("passed", 0, None)


def _security_status_failure(artifact: Artifact) -> tuple[str, str] | None:
    if artifact.error is not None:
        return "unavailable", artifact.error
    assert artifact.payload is not None
    exit_code = artifact.payload.get("exit_code")
    if not _integer(exit_code):
        return "unavailable", "invalid exit_code"
    status = artifact.payload.get("status")
    if status is not None and (not isinstance(status, str) or status not in {"passed", "failed"}):
        return "unavailable", "invalid status"
    if exit_code != 0:
        return "failed", f"exit code {exit_code}"
    if status == "failed":
        return "failed", "reported failure"
    return None


def _security_line(
    bandit: Artifact,
    audit: Artifact,
    status_artifact: Artifact | None = None,
) -> tuple[str, str]:
    bandit_status, bandit_count, bandit_detail = _security(bandit, "bandit")
    audit_status, audit_count, audit_detail = _security(audit, "audit")
    status_failure = (
        _security_status_failure(status_artifact) if status_artifact is not None else None
    )
    if status_failure is not None:
        details = ["incomplete evidence", f"{SECURITY_STATUS_ARTIFACT}: {status_failure[1]}"]
        if bandit_status != "passed" and bandit_detail is not None:
            details.append(f"Bandit: {bandit_detail}")
        if audit_status != "passed" and audit_detail is not None:
            details.append(f"pip-audit: {audit_detail}")
        label = status_failure[0].upper() if status_failure[0] == "failed" else status_failure[0]
        return status_failure[0], f"- Security: **{label}** ({'; '.join(details)})"
    if bandit_status == "passed" and audit_status == "passed":
        assert bandit_count is not None and audit_count is not None
        return (
            "passed",
            f"- Security: **{bandit_count}** medium/high Bandit findings, "
            f"**{audit_count}** dependency vulnerabilities",
        )
    details: list[str] = []
    if bandit_count is not None:
        details.append(f"{bandit_count} Bandit findings")
    if bandit_detail is not None:
        details.append(f"Bandit: {bandit_detail}")
    if audit_count is not None:
        details.append(f"{audit_count} dependency vulnerabilities")
    if audit_detail is not None:
        details.append(f"pip-audit: {audit_detail}")
    status = "failed" if "failed" in (bandit_status, audit_status) else "unavailable"
    label = status.upper() if status == "failed" else status
    if status == "unavailable" or "unavailable" in (bandit_status, audit_status):
        details.insert(0, "incomplete evidence")
    return status, f"- Security: **{label}** ({'; '.join(details)})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    coverage = _load_artifact("coverage.json")
    stability = _load_artifact("stability.json")
    build = _load_artifact("build.json")
    bandit = _load_artifact("bandit.json")
    audit = _load_artifact("pip-audit.json")
    security_status_artifact = _load_artifact(SECURITY_STATUS_ARTIFACT)
    if (
        security_status_artifact.error == f"{SECURITY_STATUS_ARTIFACT} missing"
        and os.environ.get("GITHUB_ACTIONS", "").lower() != "true"
    ):
        security_status_artifact = None
    statuses: list[str] = []
    coverage_status, coverage_line = _coverage(coverage)
    statuses.append(coverage_status)
    stability_status, stability_line = _stability(stability)
    statuses.append(stability_status)
    build_status, build_line = _build(build)
    statuses.append(build_status)
    security_status, security_line = _security_line(
        bandit,
        audit,
        security_status_artifact,
    )
    statuses.append(security_status)
    lines = [
        "## Automated quality review",
        "",
        coverage_line,
        stability_line,
        build_line,
        security_line,
        "",
        "Generated from pinned local tools. Review failures before merge.",
        "",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(args.output)
    return 1 if any(status != "passed" for status in statuses) else 0


if __name__ == "__main__":
    raise SystemExit(main())
