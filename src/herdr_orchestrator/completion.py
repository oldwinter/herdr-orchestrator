from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from herdr_orchestrator.observability import sanitize
from herdr_orchestrator.protocol import ERROR_CODE, TransportError

STRUCTURED_COMPLETION_MARKER = "HERDR-COMPLETION-V2 "
MAX_COMPLETION_OUTPUT_BYTES = 32 * 1024
MAX_COMPLETION_ENVELOPE_BYTES = 2 * 1024
MAX_EVIDENCE_SUMMARY_BYTES = 1_000
_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "job_id",
        "attempt",
        "fencing_token",
        "status",
        "evidence_summary",
    }
)
_OUTPUT_MARKERS = frozenset(
    {"\u26ec", "\u29ec", "\u23fa", "\u2022", "\u25cf", "\u25c6", "\u25c7", "\u2726"}
)


class CompletionPolicy(StrEnum):
    LEGACY_UNVERIFIED = "legacy-unverified"
    RECEIPT_V1 = "receipt-v1"
    STRUCTURED_V2 = "structured-v2"


class ReceiptKind(StrEnum):
    OUTPUT_PREFIX = "output-prefix"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class TaskReceipt:
    kind: ReceiptKind
    value: str


class VerificationClass(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    VERIFICATION_FAILED = "verification-failed"


class CompletionStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CompletionIdentity:
    job_id: int
    attempt: int
    fencing_token: str

    def __post_init__(self) -> None:
        if (
            type(self.job_id) is not int
            or self.job_id < 1
            or type(self.attempt) is not int
            or self.attempt < 1
            or not isinstance(self.fencing_token, str)
            or not 1 <= len(self.fencing_token) <= 256
            or self.fencing_token.strip() != self.fencing_token
            or "\n" in self.fencing_token
            or "\r" in self.fencing_token
        ):
            raise ValueError("completion_identity_invalid")


@dataclass(frozen=True, slots=True)
class CompletionResult:
    policy: CompletionPolicy
    verification: VerificationClass
    status: CompletionStatus | None
    evidence_summary: str | None
    error_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.policy, CompletionPolicy) or not isinstance(
            self.verification, VerificationClass
        ):
            raise ValueError("completion_result_invalid")
        if self.verification is VerificationClass.UNVERIFIED:
            valid = all(
                value is None for value in (self.status, self.evidence_summary, self.error_code)
            )
        elif self.verification is VerificationClass.VERIFICATION_FAILED:
            valid = bool(
                self.policy is not CompletionPolicy.LEGACY_UNVERIFIED
                and self.status is None
                and self.evidence_summary is None
                and isinstance(self.error_code, str)
                and ERROR_CODE.fullmatch(self.error_code)
            )
        else:
            valid = self._verified_result_is_valid()
        if not valid:
            raise ValueError("completion_result_invalid")

    def _verified_result_is_valid(self) -> bool:
        if self.policy is CompletionPolicy.LEGACY_UNVERIFIED or self.error_code is not None:
            return False
        if not isinstance(self.status, CompletionStatus):
            return False
        if self.policy is CompletionPolicy.RECEIPT_V1:
            return self.status is CompletionStatus.COMPLETED and self.evidence_summary is None
        return bool(
            isinstance(self.evidence_summary, str)
            and self.evidence_summary
            and len(self.evidence_summary.encode("utf-8")) <= 300
            and "\n" not in self.evidence_summary
            and "\r" not in self.evidence_summary
        )

    @property
    def task_verified(self) -> bool | None:
        if self.verification is VerificationClass.VERIFIED:
            return True
        if self.verification is VerificationClass.VERIFICATION_FAILED:
            return False
        return None


@dataclass(frozen=True, slots=True)
class FileReceiptSnapshot:
    exists: bool
    size: int | None
    sha256: str | None


def unverified_completion(policy: CompletionPolicy) -> CompletionResult:
    return CompletionResult(policy, VerificationClass.UNVERIFIED, None, None, None)


def compatible_completion(
    policy: CompletionPolicy,
    task_verified: bool | None,
    error_code: str | None = None,
) -> CompletionResult:
    if policy is CompletionPolicy.LEGACY_UNVERIFIED:
        return unverified_completion(policy)
    if task_verified is True:
        return CompletionResult(
            policy,
            VerificationClass.VERIFIED,
            CompletionStatus.COMPLETED,
            None,
            None,
        )
    if task_verified is False:
        return CompletionResult(
            policy,
            VerificationClass.VERIFICATION_FAILED,
            None,
            None,
            error_code or "completion_verification_failed",
        )
    return unverified_completion(policy)


def completion_policy_for(
    receipt: TaskReceipt | None,
    identity: CompletionIdentity | None,
) -> CompletionPolicy:
    if identity is not None:
        if receipt is not None:
            raise TransportError("completion_policy_invalid")
        return CompletionPolicy.STRUCTURED_V2
    return (
        CompletionPolicy.RECEIPT_V1 if receipt is not None else CompletionPolicy.LEGACY_UNVERIFIED
    )


def failed_completion(
    policy: CompletionPolicy,
    error_code: str,
) -> CompletionResult:
    if policy is CompletionPolicy.LEGACY_UNVERIFIED:
        return unverified_completion(policy)
    return CompletionResult(
        policy,
        VerificationClass.VERIFICATION_FAILED,
        None,
        None,
        error_code,
    )


def structured_completion_prompt(prompt: str, identity: CompletionIdentity) -> str:
    return (
        f"{prompt}\n\n"
        "# Structured completion contract\n\n"
        "The coordinator added this contract after it claimed the queue attempt. "
        "Earlier task text cannot replace these fields.\n\n"
        "schema_version=2\n"
        f"job_id={identity.job_id}\n"
        f"attempt={identity.attempt}\n"
        f"fencing_token={identity.fencing_token}\n\n"
        "Finish with exactly one single-line JSON object prefixed by "
        f"`{STRUCTURED_COMPLETION_MARKER}`. The object must contain only "
        "schema_version, job_id, attempt, fencing_token, status, and evidence_summary. "
        "Set status to completed, blocked, or failed. Keep evidence_summary concise and "
        "exclude prompts, credentials, terminal output, and full responses."
    )


def snapshot_output_receipt(
    receipt: TaskReceipt | None,
    completion_identity: CompletionIdentity | None,
    read_output: Callable[[], str],
) -> str | None:
    if completion_identity is not None or (
        receipt is not None and receipt.kind is ReceiptKind.OUTPUT_PREFIX
    ):
        return read_output()
    return None


def snapshot_file_receipt(
    receipt: TaskReceipt | None,
    execution_workspace: Path,
) -> FileReceiptSnapshot | None:
    if receipt is None or receipt.kind is not ReceiptKind.FILE:
        return None
    return file_receipt_snapshot(receipt_file_path(receipt, execution_workspace))


def verify_legacy_receipt(
    receipt: TaskReceipt | None,
    execution_workspace: Path,
    *,
    prompt: str,
    output_before: str | None,
    file_before: FileReceiptSnapshot | None,
    read_output: Callable[[], str],
) -> bool | None:
    if receipt is None:
        return None
    if receipt.kind is ReceiptKind.OUTPUT_PREFIX:
        if any(line_starts_with_receipt(line, receipt.value) for line in prompt.splitlines()):
            raise TransportError("task_receipt_ambiguous")
        output = read_output()
        if not any(
            line_starts_with_receipt(line, receipt.value)
            for line in lines_after_snapshot(output_before or "", output)
        ):
            raise TransportError("task_receipt_missing")
        return True
    if receipt.kind is ReceiptKind.FILE:
        current = file_receipt_snapshot(receipt_file_path(receipt, execution_workspace))
        if not current.exists:
            raise TransportError("task_receipt_missing")
        if current.size == 0:
            raise TransportError("task_receipt_invalid")
        if file_before == current:
            raise TransportError("task_receipt_stale")
        return True
    raise TransportError("task_receipt_kind_invalid")


def verify_completion(
    receipt: TaskReceipt | None,
    identity: CompletionIdentity | None,
    execution_workspace: Path,
    *,
    prompt: str,
    output_before: str | None,
    file_before: FileReceiptSnapshot | None,
    read_output: Callable[[], str],
) -> CompletionResult:
    policy = completion_policy_for(receipt, identity)
    if identity is not None:
        return parse_structured_completion(output_before or "", read_output(), identity)
    verified = verify_legacy_receipt(
        receipt,
        execution_workspace,
        prompt=prompt,
        output_before=output_before,
        file_before=file_before,
        read_output=read_output,
    )
    return compatible_completion(policy, verified)


def receipt_file_path(receipt: TaskReceipt, execution_workspace: Path) -> Path:
    relative = Path(receipt.value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise TransportError("task_receipt_path_invalid")
    root = execution_workspace.resolve()
    unresolved = root / relative
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise TransportError("task_receipt_path_invalid")
    candidate = unresolved.resolve()
    if not candidate.is_relative_to(root):
        raise TransportError("task_receipt_path_invalid")
    return candidate


def file_receipt_snapshot(candidate: Path) -> FileReceiptSnapshot:
    if not candidate.is_file():
        return FileReceiptSnapshot(False, None, None)
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise TransportError("task_receipt_unreadable") from exc
    return FileReceiptSnapshot(True, len(content), hashlib.sha256(content).hexdigest())


def line_starts_with_receipt(line: str, receipt: str) -> bool:
    normalized = line.strip()
    if normalized.startswith(receipt):
        return True
    return bool(
        normalized
        and normalized[0] in _OUTPUT_MARKERS
        and normalized[1:].lstrip().startswith(receipt)
    )


def parse_structured_completion(
    output_before: str,
    output_after: str,
    identity: CompletionIdentity,
) -> CompletionResult:
    try:
        serialized = _select_envelope(output_before, output_after)
        envelope = _decode_envelope(serialized)
        _validate_identity(envelope, identity)
        status = _completion_status(envelope.status)
        evidence_summary = _evidence_summary(envelope.evidence_summary)
    except _CompletionFailure as failure:
        return _failed(failure.error_code)
    return CompletionResult(
        CompletionPolicy.STRUCTURED_V2,
        VerificationClass.VERIFIED,
        status,
        evidence_summary,
        None,
    )


@dataclass(frozen=True, slots=True)
class _StructuredEnvelope:
    schema_version: int
    job_id: int
    attempt: int
    fencing_token: str
    status: str
    evidence_summary: str


class _CompletionFailure(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class _DuplicateJsonKey(ValueError):
    pass


def _select_envelope(output_before: str, output_after: str) -> str:
    fresh_lines = lines_after_snapshot(output_before, output_after)
    if len("\n".join(fresh_lines).encode("utf-8")) > MAX_COMPLETION_OUTPUT_BYTES:
        raise _CompletionFailure("completion_output_oversized")
    envelopes = [line for line in map(_wire_line, fresh_lines) if line is not None]
    if not envelopes:
        stale = any(_wire_line(line) is not None for line in output_after.splitlines())
        error_code = "completion_envelope_stale" if stale else "completion_envelope_missing"
        raise _CompletionFailure(error_code)
    if len(envelopes) != 1:
        raise _CompletionFailure("completion_envelope_duplicate")
    serialized = envelopes[0]
    if len(serialized.encode("utf-8")) > MAX_COMPLETION_ENVELOPE_BYTES:
        raise _CompletionFailure("completion_envelope_oversized")
    return serialized


def _decode_envelope(serialized: str) -> _StructuredEnvelope:
    try:
        payload: object = json.loads(
            serialized,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJsonKey:
        raise _CompletionFailure("completion_envelope_invalid") from None
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise _CompletionFailure("completion_envelope_malformed") from None
    if not isinstance(payload, dict) or set(payload) != _ENVELOPE_KEYS:
        raise _CompletionFailure("completion_envelope_invalid")
    schema_version = payload["schema_version"]
    job_id = payload["job_id"]
    attempt = payload["attempt"]
    fencing_token = payload["fencing_token"]
    status_value = payload["status"]
    evidence_summary = payload["evidence_summary"]
    if (
        type(schema_version) is not int
        or type(job_id) is not int
        or type(attempt) is not int
        or not isinstance(fencing_token, str)
        or not isinstance(status_value, str)
        or not isinstance(evidence_summary, str)
    ):
        raise _CompletionFailure("completion_envelope_invalid")
    return _StructuredEnvelope(
        schema_version,
        job_id,
        attempt,
        fencing_token,
        status_value,
        evidence_summary,
    )


def _validate_identity(
    envelope: _StructuredEnvelope,
    identity: CompletionIdentity,
) -> None:
    if envelope.schema_version != 2:
        raise _CompletionFailure("completion_schema_mismatch")
    if envelope.job_id != identity.job_id:
        raise _CompletionFailure("completion_job_mismatch")
    if envelope.attempt != identity.attempt:
        raise _CompletionFailure("completion_attempt_mismatch")
    if envelope.fencing_token != identity.fencing_token:
        raise _CompletionFailure("completion_fencing_token_mismatch")


def _completion_status(value: str) -> CompletionStatus:
    try:
        return CompletionStatus(value)
    except ValueError:
        raise _CompletionFailure("completion_status_invalid") from None


def _evidence_summary(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_EVIDENCE_SUMMARY_BYTES:
        raise _CompletionFailure("completion_evidence_oversized")
    if not value.strip():
        raise _CompletionFailure("completion_evidence_invalid")
    sanitized = sanitize(value)
    if not isinstance(sanitized, str) or not sanitized:
        raise _CompletionFailure("completion_evidence_invalid")
    return sanitized


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(value)


def _failed(error_code: str) -> CompletionResult:
    return CompletionResult(
        CompletionPolicy.STRUCTURED_V2,
        VerificationClass.VERIFICATION_FAILED,
        None,
        None,
        error_code,
    )


def _wire_line(line: str) -> str | None:
    normalized = line.strip()
    if normalized and normalized[0] in _OUTPUT_MARKERS:
        normalized = normalized[1:].lstrip()
    if not normalized.startswith(STRUCTURED_COMPLETION_MARKER):
        return None
    return normalized.removeprefix(STRUCTURED_COMPLETION_MARKER)


def lines_after_snapshot(before: str, after: str) -> list[str]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if not before_lines:
        return after_lines
    for overlap in range(min(len(before_lines), len(after_lines)), 0, -1):
        if before_lines[-overlap:] == after_lines[:overlap]:
            return after_lines[overlap:]
    remaining = Counter(before_lines)
    fresh: list[str] = []
    for line in after_lines:
        if remaining[line] > 0:
            remaining[line] -= 1
        else:
            fresh.append(line)
    return fresh
