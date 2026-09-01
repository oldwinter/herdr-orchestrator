from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.model import Harness, WorkflowConfig

_COMMIT = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
_PACKAGE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,127}\Z")


class ReadinessVerification(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT VERIFIED"


class ReadinessStatus(StrEnum):
    READY = "ready"
    AUTH_REQUIRED = "auth_required"
    MODEL_INVALID = "model_invalid"
    TIMEOUT = "timeout"
    ERROR = "error"
    UNAVAILABLE = "unavailable"
    EXPIRED = "expired"


class ReadinessErrorCode(StrEnum):
    HARNESS_UNAVAILABLE = "harness_unavailable"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    NOT_IN_HERDR = "not_in_herdr"
    AGENT_AUTH_FAILED = "agent_auth_failed"
    AGENT_AUTH_REQUIRED = "agent_auth_required"
    AGENT_MODEL_INVALID = "agent_model_invalid"
    AGENT_BLOCKED = "agent_blocked"
    AGENT_IDENTITY_MISMATCH = "agent_identity_mismatch"
    AGENT_NOT_READY = "agent_not_ready"
    AGENT_NOT_SETTLED = "agent_not_settled"
    AGENT_PANE_MISMATCH = "agent_pane_mismatch"
    AGENT_START_FAILED = "agent_start_failed"
    AGENT_WORKSPACE_MISMATCH = "agent_workspace_mismatch"
    HERDR_TIMEOUT = "herdr_timeout"
    TIMEOUT = "timeout"
    PROMPT_ACCEPTANCE_TIMEOUT = "prompt_acceptance_timeout"
    AGENT_PROVIDER_FAILED = "agent_provider_failed"
    HERDR_UNAVAILABLE = "herdr_unavailable"
    AGENT_TURN_NOT_OBSERVED = "agent_turn_not_observed"
    HERDR_INVALID_RESPONSE = "herdr_invalid_response"
    HERDR_PANE_ID_MISSING = "herdr_pane_id_missing"
    HERDR_WORKSPACE_ID_MISSING = "herdr_workspace_id_missing"
    PANE_SHELL_NOT_READY = "pane_shell_not_ready"
    TASK_RECEIPT_AMBIGUOUS = "task_receipt_ambiguous"
    TASK_RECEIPT_INVALID = "task_receipt_invalid"
    TASK_RECEIPT_KIND_INVALID = "task_receipt_kind_invalid"
    TASK_RECEIPT_MISSING = "task_receipt_missing"
    TASK_RECEIPT_PATH_INVALID = "task_receipt_path_invalid"
    TASK_RECEIPT_STALE = "task_receipt_stale"
    TASK_RECEIPT_UNREADABLE = "task_receipt_unreadable"
    READINESS_PROBE_FAILED = "readiness_probe_failed"
    READINESS_RESULT_INVALID = "readiness_result_invalid"
    READINESS_EVIDENCE_EXPIRED = "readiness_evidence_expired"
    READINESS_CI_FORBIDDEN = "readiness_ci_forbidden"
    READINESS_SOURCE_DIRTY = "readiness_source_dirty"
    READINESS_SOURCE_CHANGED = "readiness_source_changed"


@dataclass(frozen=True, slots=True)
class _ReadinessErrorPolicy:
    status: ReadinessStatus
    retryable: bool = False


_ERROR_POLICY: dict[ReadinessErrorCode, _ReadinessErrorPolicy] = {
    ReadinessErrorCode.HARNESS_UNAVAILABLE: _ReadinessErrorPolicy(ReadinessStatus.UNAVAILABLE),
    ReadinessErrorCode.PROFILE_UNAVAILABLE: _ReadinessErrorPolicy(ReadinessStatus.UNAVAILABLE),
    ReadinessErrorCode.NOT_IN_HERDR: _ReadinessErrorPolicy(ReadinessStatus.UNAVAILABLE),
    ReadinessErrorCode.HERDR_UNAVAILABLE: _ReadinessErrorPolicy(ReadinessStatus.UNAVAILABLE),
    ReadinessErrorCode.AGENT_AUTH_FAILED: _ReadinessErrorPolicy(ReadinessStatus.AUTH_REQUIRED),
    ReadinessErrorCode.AGENT_AUTH_REQUIRED: _ReadinessErrorPolicy(ReadinessStatus.AUTH_REQUIRED),
    ReadinessErrorCode.AGENT_MODEL_INVALID: _ReadinessErrorPolicy(ReadinessStatus.MODEL_INVALID),
    ReadinessErrorCode.AGENT_BLOCKED: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.AGENT_IDENTITY_MISMATCH: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.AGENT_NOT_READY: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.AGENT_NOT_SETTLED: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.AGENT_PANE_MISMATCH: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.AGENT_START_FAILED: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.AGENT_WORKSPACE_MISMATCH: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.HERDR_TIMEOUT: _ReadinessErrorPolicy(ReadinessStatus.TIMEOUT, True),
    ReadinessErrorCode.TIMEOUT: _ReadinessErrorPolicy(ReadinessStatus.TIMEOUT, True),
    ReadinessErrorCode.PROMPT_ACCEPTANCE_TIMEOUT: _ReadinessErrorPolicy(
        ReadinessStatus.TIMEOUT, True
    ),
    ReadinessErrorCode.AGENT_PROVIDER_FAILED: _ReadinessErrorPolicy(ReadinessStatus.ERROR, True),
    ReadinessErrorCode.AGENT_TURN_NOT_OBSERVED: _ReadinessErrorPolicy(ReadinessStatus.ERROR, True),
    ReadinessErrorCode.HERDR_INVALID_RESPONSE: _ReadinessErrorPolicy(ReadinessStatus.ERROR, True),
    ReadinessErrorCode.HERDR_PANE_ID_MISSING: _ReadinessErrorPolicy(ReadinessStatus.UNAVAILABLE),
    ReadinessErrorCode.HERDR_WORKSPACE_ID_MISSING: _ReadinessErrorPolicy(
        ReadinessStatus.UNAVAILABLE
    ),
    ReadinessErrorCode.PANE_SHELL_NOT_READY: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.TASK_RECEIPT_AMBIGUOUS: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.TASK_RECEIPT_INVALID: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.TASK_RECEIPT_KIND_INVALID: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.TASK_RECEIPT_MISSING: _ReadinessErrorPolicy(ReadinessStatus.ERROR, True),
    ReadinessErrorCode.TASK_RECEIPT_PATH_INVALID: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.TASK_RECEIPT_STALE: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.TASK_RECEIPT_UNREADABLE: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.READINESS_PROBE_FAILED: _ReadinessErrorPolicy(ReadinessStatus.ERROR, True),
    ReadinessErrorCode.READINESS_RESULT_INVALID: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.READINESS_EVIDENCE_EXPIRED: _ReadinessErrorPolicy(ReadinessStatus.EXPIRED),
    ReadinessErrorCode.READINESS_CI_FORBIDDEN: _ReadinessErrorPolicy(ReadinessStatus.UNAVAILABLE),
    ReadinessErrorCode.READINESS_SOURCE_DIRTY: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
    ReadinessErrorCode.READINESS_SOURCE_CHANGED: _ReadinessErrorPolicy(ReadinessStatus.ERROR),
}


@dataclass(frozen=True, slots=True)
class BuildIdentity:
    commit: str
    package_version: str
    source_clean: bool = True

    def __post_init__(self) -> None:
        if _COMMIT.fullmatch(self.commit) is None:
            raise ValueError("readiness_commit_invalid")
        if _PACKAGE_VERSION.fullmatch(self.package_version) is None:
            raise ValueError("readiness_package_version_invalid")
        if not isinstance(self.source_clean, bool):
            raise ValueError("readiness_source_state_invalid")


@dataclass(frozen=True, slots=True)
class ReadinessEnvironment:
    managed_pane: bool
    executable_available: Mapping[Harness, bool]
    profile_available: Mapping[Harness, bool]
    ci: bool = False


def inspect_readiness_environment(
    workflow: WorkflowConfig,
    *,
    environ: Mapping[str, str],
    which: Callable[[str], str | None],
) -> ReadinessEnvironment:
    managed_pane = bool(
        environ.get("HERDR_ENV") == "1"
        and environ.get("HERDR_PANE_ID")
        and environ.get("HERDR_WORKSPACE_ID")
        and which("herdr") is not None
    )
    executable_available = {harness: which(harness.value) is not None for harness in Harness}
    profile_available = {
        profile.harness: profile.context_file.is_file() for profile in workflow.profiles
    }
    ci = _environment_flag(environ.get("CI")) or _environment_flag(environ.get("GITHUB_ACTIONS"))
    return ReadinessEnvironment(managed_pane, executable_available, profile_available, ci)


def resolve_build_identity(
    workspace: Path,
    package_version: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> BuildIdentity:
    try:
        commit_process = runner(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("readiness_commit_unavailable") from exc
    if commit_process.returncode != 0:
        raise ValueError("readiness_commit_unavailable")
    try:
        status_process = runner(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        source_clean = False
    else:
        try:
            confirm_process = runner(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            source_clean = False
        else:
            source_clean = bool(
                status_process.returncode == 0
                and not status_process.stdout.strip()
                and confirm_process.returncode == 0
                and confirm_process.stdout.strip() == commit_process.stdout.strip()
            )
    return BuildIdentity(commit_process.stdout.strip(), package_version, source_clean)


@dataclass(frozen=True, slots=True)
class ReadinessPhaseTimings:
    provision_ready: int | None = None
    receipt_baseline: int | None = None
    turn_settlement: int | None = None
    receipt_verification: int | None = None
    total: int | None = None

    @classmethod
    def parse(cls, raw: object) -> ReadinessPhaseTimings | None:
        if not isinstance(raw, Mapping):
            return None
        expected = {
            "provision_ready",
            "receipt_baseline",
            "turn_settlement",
            "receipt_verification",
            "total",
        }
        if any(not isinstance(key, str) or key not in expected for key in raw):
            return None
        values: dict[str, int] = {}
        for key, value in raw.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            values[str(key)] = value
        return cls(**values)

    def public_json(self) -> dict[str, int]:
        values = {
            "provision_ready": self.provision_ready,
            "receipt_baseline": self.receipt_baseline,
            "turn_settlement": self.turn_settlement,
            "receipt_verification": self.receipt_verification,
            "total": self.total,
        }
        return {key: value for key, value in values.items() if value is not None}


class ReadinessProbe(Protocol):
    def __call__(
        self,
        workflow: WorkflowConfig,
        harness: Harness,
        timeout_seconds: int,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ReadinessEntry:
    harness: Harness
    status: ReadinessStatus
    error_code: ReadinessErrorCode | None
    phase_timings: ReadinessPhaseTimings
    observed_at: datetime
    attempt_count: int

    def __post_init__(self) -> None:
        expected_status = (
            ReadinessStatus.READY
            if self.error_code is None
            else _ERROR_POLICY[self.error_code].status
        )
        if (
            self.status is not expected_status
            or not 0 <= self.attempt_count <= 2
            or self.observed_at.tzinfo is None
        ):
            raise ValueError("readiness_entry_invalid")

    @property
    def verification(self) -> ReadinessVerification:
        if self.status is ReadinessStatus.READY and self.error_code is None:
            return ReadinessVerification.VERIFIED
        return ReadinessVerification.NOT_VERIFIED

    def public_json(self) -> dict[str, object]:
        return {
            "harness": self.harness.value,
            "status": self.status.value,
            "verification": self.verification.value,
            "error_code": self.error_code.value if self.error_code is not None else None,
            "phase_timings_ms": self.phase_timings.public_json(),
            "observed_at": _timestamp(self.observed_at),
            "attempt_count": self.attempt_count,
        }


@dataclass(frozen=True, slots=True)
class ReadinessMatrix:
    build: BuildIdentity
    workflow: str
    workspace_id: str
    observed_at: datetime
    results: tuple[ReadinessEntry, ...]

    @property
    def verification(self) -> ReadinessVerification:
        if self.results and all(
            result.verification is ReadinessVerification.VERIFIED for result in self.results
        ):
            return ReadinessVerification.VERIFIED
        return ReadinessVerification.NOT_VERIFIED

    def public_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "commit": self.build.commit,
            "package_version": self.build.package_version,
            "source_clean": self.build.source_clean,
            "workflow": self.workflow,
            "workspace_id": self.workspace_id,
            "observed_at": _timestamp(self.observed_at),
            "verification": self.verification.value,
            "results": [result.public_json() for result in self.results],
        }

    def invalidate_source_change(self, observed_at: datetime) -> ReadinessMatrix:
        return ReadinessMatrix(
            BuildIdentity(self.build.commit, self.build.package_version, source_clean=False),
            self.workflow,
            self.workspace_id,
            _utc(observed_at),
            tuple(
                _error_entry(
                    result.harness,
                    ReadinessErrorCode.READINESS_SOURCE_CHANGED,
                    result.phase_timings,
                    observed_at,
                    result.attempt_count,
                )
                for result in self.results
            ),
        )


def collect_readiness_matrix(
    workflow: WorkflowConfig,
    *,
    selected_harnesses: Sequence[str] | None,
    timeout_seconds: int,
    environment: ReadinessEnvironment,
    build: BuildIdentity,
    probe: ReadinessProbe,
    clock: Callable[[], datetime],
) -> ReadinessMatrix:
    if not 5 <= timeout_seconds <= 300:
        raise ValueError("readiness_probe_timeout_out_of_range")
    started_at = _utc(clock())
    harnesses = _selected_harnesses(workflow, selected_harnesses)
    entries: list[ReadinessEntry] = []
    for harness in harnesses:
        preflight_error = _preflight_error(environment, build, harness)
        if preflight_error is not None:
            entries.append(
                _error_entry(
                    harness,
                    preflight_error,
                    ReadinessPhaseTimings(),
                    started_at,
                    0,
                )
            )
            continue
        entries.append(
            _probe_with_retry(
                workflow,
                harness,
                timeout_seconds,
                probe=probe,
                started_at=started_at,
                clock=clock,
            )
        )
    return ReadinessMatrix(
        build,
        workflow.name,
        hashlib.sha256(str(workflow.workspace.resolve()).encode()).hexdigest()[:16],
        _utc(clock()),
        tuple(entries),
    )


def _selected_harnesses(
    workflow: WorkflowConfig,
    selected: Sequence[str] | None,
) -> tuple[Harness, ...]:
    enabled = list(dict.fromkeys(worker.harness for worker in workflow.workers))
    planner = workflow.planner.harness
    if planner is not None and planner not in enabled:
        enabled.append(planner)
    if not selected:
        return tuple(enabled)
    requested = tuple(dict.fromkeys(Harness(value) for value in selected))
    unavailable = [harness.value for harness in requested if harness not in enabled]
    if unavailable:
        raise ValueError(f"readiness_harness_not_enabled: {','.join(unavailable)}")
    return requested


def _preflight_error(
    environment: ReadinessEnvironment,
    build: BuildIdentity,
    harness: Harness,
) -> ReadinessErrorCode | None:
    if environment.ci:
        return ReadinessErrorCode.READINESS_CI_FORBIDDEN
    if not build.source_clean:
        return ReadinessErrorCode.READINESS_SOURCE_DIRTY
    if not environment.managed_pane:
        return ReadinessErrorCode.NOT_IN_HERDR
    if not environment.executable_available.get(harness, False):
        return ReadinessErrorCode.HARNESS_UNAVAILABLE
    if not environment.profile_available.get(harness, False):
        return ReadinessErrorCode.PROFILE_UNAVAILABLE
    return None


def _environment_flag(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes"}


def _probe_with_retry(
    workflow: WorkflowConfig,
    harness: Harness,
    timeout_seconds: int,
    *,
    probe: ReadinessProbe,
    started_at: datetime,
    clock: Callable[[], datetime],
) -> ReadinessEntry:
    for attempt in (1, 2):
        result = _probe_once(
            workflow,
            harness,
            timeout_seconds,
            probe=probe,
            attempt=attempt,
            started_at=started_at,
            clock=clock,
        )
        if (
            attempt == 2
            or result.error_code is None
            or not _ERROR_POLICY[result.error_code].retryable
        ):
            return result
    raise AssertionError("readiness_retry_exhausted")


def _probe_once(
    workflow: WorkflowConfig,
    harness: Harness,
    timeout_seconds: int,
    *,
    probe: ReadinessProbe,
    attempt: int,
    started_at: datetime,
    clock: Callable[[], datetime],
) -> ReadinessEntry:
    try:
        raw = probe(workflow, harness, timeout_seconds)
    except Exception:
        return _error_entry(
            harness,
            ReadinessErrorCode.READINESS_PROBE_FAILED,
            ReadinessPhaseTimings(),
            _utc(clock()),
            attempt,
        )
    observed_at = _utc(clock())
    return _parse_probe_result(
        harness,
        raw,
        attempt=attempt,
        started_at=started_at,
        observed_at=observed_at,
    )


def _parse_probe_result(
    harness: Harness,
    raw: object,
    *,
    attempt: int,
    started_at: datetime,
    observed_at: datetime,
) -> ReadinessEntry:
    if not isinstance(raw, Mapping):
        return _invalid_entry(harness, attempt, observed_at)
    try:
        status = ReadinessStatus(str(raw.get("status")))
    except ValueError:
        return _invalid_entry(harness, attempt, observed_at)
    raw_error = raw.get("error_code")
    if raw_error is None:
        error_code = None
    else:
        try:
            error_code = ReadinessErrorCode(str(raw_error))
        except ValueError:
            return _invalid_entry(harness, attempt, observed_at)
    expected_status = (
        ReadinessStatus.READY if error_code is None else _ERROR_POLICY[error_code].status
    )
    if status is not expected_status:
        return _invalid_entry(harness, attempt, observed_at)
    timings = ReadinessPhaseTimings.parse(raw.get("phase_timings_ms", {}))
    if timings is None or timings.total is None:
        return _invalid_entry(harness, attempt, observed_at)
    raw_timestamp = raw.get("observed_at")
    if raw_timestamp is not None:
        parsed = _parse_timestamp(raw_timestamp)
        if parsed is None:
            return _invalid_entry(harness, attempt, observed_at)
        evidence_time = parsed
    else:
        evidence_time = observed_at
    if evidence_time < started_at or evidence_time > observed_at:
        return _error_entry(
            harness,
            ReadinessErrorCode.READINESS_EVIDENCE_EXPIRED,
            timings,
            evidence_time,
            attempt,
        )
    return ReadinessEntry(harness, status, error_code, timings, evidence_time, attempt)


def _invalid_entry(
    harness: Harness,
    attempt: int,
    observed_at: datetime,
) -> ReadinessEntry:
    return _error_entry(
        harness,
        ReadinessErrorCode.READINESS_RESULT_INVALID,
        ReadinessPhaseTimings(),
        observed_at,
        attempt,
    )


def _error_entry(
    harness: Harness,
    error_code: ReadinessErrorCode,
    phase_timings: ReadinessPhaseTimings,
    observed_at: datetime,
    attempt_count: int,
) -> ReadinessEntry:
    return ReadinessEntry(
        harness,
        _ERROR_POLICY[error_code].status,
        error_code,
        phase_timings,
        _utc(observed_at),
        attempt_count,
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("readiness_clock_timezone_missing")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")
