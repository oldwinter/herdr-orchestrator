from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from herdr_orchestrator.delivery_journal import (
    DeliveryEffectObservation,
    DeliveryEffectState,
)
from herdr_orchestrator.delivery_protocol import (
    DeliveryArtifactError,
    ReviewFinding,
    ReviewReport,
    validate_artifact_path,
    write_artifact_text,
)
from herdr_orchestrator.git_workspace import GitWorkspace, GitWorkspaceError, Worktree
from herdr_orchestrator.model import AgentState, DispatchOutcome
from herdr_orchestrator.tracker import contains_high_confidence_secret


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    run_id: str
    status: str
    artifact_root: Path
    tracker_references: dict[str, str]
    integration_branch: str
    integration_commit: str
    tickets_completed: int
    review_rounds: int


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _finding_map(report: ReviewReport) -> dict[str, ReviewFinding]:
    findings: dict[str, ReviewFinding] = {}
    for axis, rows in (("standards", report.standards), ("spec", report.spec)):
        for index, finding in enumerate(rows, 1):
            findings[f"{axis}:{index}"] = finding
    return findings


def _journal_payload(value: dict[str, object]) -> dict[str, object]:
    if contains_high_confidence_secret(value):
        raise DeliveryError("delivery_journal_sensitive_value")
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        if len(serialized.encode("utf-8")) > 64 * 1024:
            raise DeliveryError("delivery_journal_payload_too_large")
        payload = json.loads(serialized)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DeliveryError("delivery_journal_payload_invalid") from exc
    if not isinstance(payload, dict):
        raise DeliveryError("delivery_journal_payload_invalid")
    return payload


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DeliveryError("delivery_artifact_unreadable") from exc


def _effect_absent() -> DeliveryEffectObservation:
    return DeliveryEffectObservation(DeliveryEffectState.ABSENT)


def _effect_matched(details: dict[str, object]) -> DeliveryEffectObservation:
    return DeliveryEffectObservation(DeliveryEffectState.MATCHED, details)


def _effect_conflict() -> DeliveryEffectObservation:
    return DeliveryEffectObservation(DeliveryEffectState.CONFLICT)


def _agent_is_active(outcome: DispatchOutcome | None) -> bool:
    return outcome is not None and outcome.state in {
        AgentState.WORKING,
        AgentState.BLOCKED,
        AgentState.UNKNOWN,
    }


def _require_success(outcome: DispatchOutcome, role: str) -> None:
    if outcome.error_code is not None or outcome.state not in {
        AgentState.IDLE,
        AgentState.DONE,
    }:
        raise DeliveryError(
            f"delivery_dispatch_failed:{role}:" f"{outcome.error_code or outcome.state.value}"
        )


def _safe_delivery_path(path: Path, *, root: Path | None = None) -> Path:
    try:
        return validate_artifact_path(path, root=root)
    except DeliveryArtifactError as exc:
        raise DeliveryError("delivery_artifact_path_invalid") from exc


def _validate_worktree_ownership(
    git: GitWorkspace,
    expected_path: Path,
    worktree: Worktree,
) -> None:
    try:
        git.validate_ownership(expected_path, worktree)
    except GitWorkspaceError as exc:
        raise DeliveryError(str(exc)) from exc


def _validate_worktree_clean(git: GitWorkspace, path: Path) -> None:
    try:
        git.validate_clean(path)
    except GitWorkspaceError as exc:
        raise DeliveryError(str(exc)) from exc


def _git_output(git: GitWorkspace, cwd: Path, *args: str) -> str:
    try:
        return git.output(cwd, *args)
    except GitWorkspaceError as exc:
        raise DeliveryError(str(exc)) from exc


def _git_succeeds(git: GitWorkspace, cwd: Path, *args: str) -> bool:
    try:
        return git.succeeds(cwd, *args)
    except GitWorkspaceError as exc:
        raise DeliveryError("delivery_git_query_failed") from exc


def _write_json(path: Path, payload: dict[str, object]) -> None:
    write_artifact_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        error_type=DeliveryError,
    )


def _load_completed_result(path: Path, run_id: str) -> DeliveryResult | None:
    _safe_delivery_path(path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeliveryError("delivery_result_invalid_json") from exc
    expected = {
        "run_id",
        "status",
        "artifact_root",
        "tracker_references",
        "integration_branch",
        "integration_commit",
        "tickets_completed",
        "review_rounds",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise DeliveryError("delivery_result_invalid_shape")
    references = payload["tracker_references"]
    artifact_root = payload["artifact_root"]
    if (
        payload["run_id"] != run_id
        or payload["status"] != "succeeded"
        or not isinstance(references, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in references.items()
        )
        or not isinstance(artifact_root, str)
        or Path(artifact_root).resolve() != path.parent.resolve()
        or not isinstance(payload["integration_branch"], str)
        or not isinstance(payload["integration_commit"], str)
        or not isinstance(payload["tickets_completed"], int)
        or isinstance(payload["tickets_completed"], bool)
        or not isinstance(payload["review_rounds"], int)
        or isinstance(payload["review_rounds"], bool)
    ):
        raise DeliveryError("delivery_result_invalid")
    return DeliveryResult(
        run_id=run_id,
        status="succeeded",
        artifact_root=Path(artifact_root),
        tracker_references=dict(references),
        integration_branch=payload["integration_branch"],
        integration_commit=payload["integration_commit"],
        tickets_completed=payload["tickets_completed"],
        review_rounds=payload["review_rounds"],
    )
