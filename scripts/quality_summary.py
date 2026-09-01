#!/usr/bin/env python3
"""Summarize machine-readable quality results for humans and PR automation."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path


def _load_quality_bundle_module():
    name = "_quality_bundle_for_summary"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).with_name("quality_bundle.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


quality_bundle = _load_quality_bundle_module()

COVERAGE_THRESHOLD = 80.0
DISPLAY_PRODUCERS = ("lint", "coverage", "stability", "security", "build", "profiling")


@dataclass(frozen=True)
class Artifact:
    payload: dict[str, object] | None
    error: str | None = None


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
    if label == "npm":
        metadata = artifact.payload.get("metadata")
        if not isinstance(metadata, dict):
            return "unavailable", None, "metadata missing"
        counts = metadata.get("vulnerabilities")
        if not isinstance(counts, dict) or not _integer(counts.get("total")):
            return "unavailable", None, "vulnerability counts missing"
        total = counts["total"]
        return ("failed", total, "vulnerabilities") if total else ("passed", 0, None)
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


def _security_line(
    bandit: Artifact,
    audit: Artifact,
    npm_audits: Sequence[Artifact] = (),
) -> tuple[str, str]:
    bandit_status, bandit_count, bandit_detail = _security(bandit, "bandit")
    audit_status, audit_count, audit_detail = _security(audit, "audit")
    npm_results = [_security(artifact, "npm") for artifact in npm_audits]
    if (
        bandit_status == "passed"
        and audit_status == "passed"
        and all(status == "passed" for status, _, _ in npm_results)
    ):
        assert bandit_count is not None and audit_count is not None
        npm_count = sum(count or 0 for _, count, _ in npm_results)
        return (
            "passed",
            f"- Security: **{bandit_count}** medium/high Bandit findings, "
            f"**{audit_count + npm_count}** dependency vulnerabilities",
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
    for index, (_status, count, detail) in enumerate(npm_results, 1):
        if count is not None:
            details.append(f"npm audit {index}: {count} dependency vulnerabilities")
        if detail is not None:
            details.append(f"npm audit {index}: {detail}")
    statuses = (bandit_status, audit_status, *(status for status, _, _ in npm_results))
    status = "failed" if "failed" in statuses else "unavailable"
    label = status.upper() if status == "failed" else status
    if status == "unavailable" or "unavailable" in statuses:
        details.insert(0, "incomplete evidence")
    return status, f"- Security: **{label}** ({'; '.join(details)})"


def _not_verified(label: str, detail: str) -> tuple[str, str]:
    return "not_verified", f"- {label}: **NOT VERIFIED** ({detail})"


def _write_output(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _manifest_artifact(producer: object, key: str) -> Artifact:
    for artifact in producer.artifacts:
        if artifact.key == key and artifact.verified and artifact.payload is not None:
            return Artifact(artifact.payload)
    return Artifact(None, f"{key} artifact missing")


def _render_manifest(manifest: object) -> tuple[int, str]:
    producers = {producer.name: producer for producer in manifest.producers}
    statuses: list[str] = []
    lines = ["## Automated quality review", ""]

    for name in DISPLAY_PRODUCERS:
        producer = producers.get(name)
        label = {
            "lint": "Static analysis",
            "coverage": "Coverage",
            "stability": "Stability",
            "security": "Security",
            "build": "Build",
            "profiling": "Profiling",
        }[name]
        if producer is None:
            status, line = _not_verified(label, "producer missing")
        elif producer.verification != "verified" or producer.outcome != "passed":
            status, line = _not_verified(label, "producer failed")
        elif name == "coverage":
            status, line = _coverage(_manifest_artifact(producer, "coverage"))
        elif name == "stability":
            status, line = _stability(_manifest_artifact(producer, "stability"))
        elif name == "build":
            status, line = _build(_manifest_artifact(producer, "build"))
        elif name == "security":
            status, line = _security_line(
                _manifest_artifact(producer, "bandit"),
                _manifest_artifact(producer, "pip-audit"),
                (
                    _manifest_artifact(producer, "npm-audit-root"),
                    _manifest_artifact(producer, "npm-audit-manager"),
                ),
            )
        else:
            status, line = "passed", f"- {label}: **verified**"
        if status != "passed":
            detail = line.split("(", maxsplit=1)[-1].rstrip(")") if "(" in line else status
            status, line = _not_verified(label, detail)
        statuses.append(status)
        lines.append(line)

    lines.extend(
        (
            "",
            f"Commit: `{manifest.commit}`",
            f"Invocation: `{manifest.invocation_id}`",
            f"Run: `{manifest.run_id}`",
            "",
        )
    )
    return (1 if any(status != "passed" for status in statuses) else 0), "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--result", type=Path)
    source.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-invocation")
    parser.add_argument("--expected-run")
    parser.add_argument("--expected-source")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(
            os.environ.get("QUALITY_EVIDENCE_ROOT", quality_bundle.ROOT / ".orchestrator/quality")
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.result is not None:
            result = quality_bundle.load_run_result(args.result, expected_root=args.root)
            manifest = quality_bundle.load_manifest_from_result(
                result,
                require_clean=args.require_clean,
                expected_root=args.root,
            )
        else:
            expectations = (
                args.expected_commit,
                args.expected_invocation,
                args.expected_run,
                args.expected_source,
            )
            if any(value is None for value in expectations):
                parser.error("direct --manifest requires all --expected-* identity arguments")
            manifest = quality_bundle.load_completed_manifest(
                args.manifest,
                expected_commit=args.expected_commit,
                expected_invocation_id=args.expected_invocation,
                expected_run_id=args.expected_run,
                expected_source_digest=args.expected_source,
                require_clean=args.require_clean,
                expected_root=args.root,
            )
    except quality_bundle.QualityBundleError as error:
        try:
            _write_output(
                args.output,
                "\n".join(
                    (
                        "## Automated quality review",
                        "",
                        f"- Evidence: **NOT VERIFIED** ({error})",
                        "",
                    )
                ),
            )
        except (OSError, RuntimeError):
            print("quality_storage_unavailable", file=sys.stderr)
            return 2
        print(args.output)
        return 1
    status, summary = _render_manifest(manifest)
    try:
        _write_output(args.output, summary)
    except (OSError, RuntimeError):
        print("quality_storage_unavailable", file=sys.stderr)
        return 2
    print(args.output)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
