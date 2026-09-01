from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from herdr_orchestrator.observability import sanitize
from herdr_orchestrator.protocol import TransportError

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


@dataclass(frozen=True, slots=True)
class CompletionResult:
    policy: CompletionPolicy
    verification: VerificationClass
    status: CompletionStatus | None
    evidence_summary: str | None
    error_code: str | None

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
    fresh_lines = lines_after_snapshot(output_before, output_after)
    if len("\n".join(fresh_lines).encode("utf-8")) > MAX_COMPLETION_OUTPUT_BYTES:
        return _failed("completion_output_oversized")
    envelopes = [line for line in map(_wire_line, fresh_lines) if line is not None]
    if not envelopes:
        stale = any(_wire_line(line) is not None for line in output_after.splitlines())
        return _failed("completion_envelope_stale" if stale else "completion_envelope_missing")
    if len(envelopes) != 1:
        return _failed("completion_envelope_duplicate")
    serialized = envelopes[0]
    if len(serialized.encode("utf-8")) > MAX_COMPLETION_ENVELOPE_BYTES:
        return _failed("completion_envelope_oversized")
    try:
        payload: object = json.loads(
            serialized,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJsonKey:
        return _failed("completion_envelope_invalid")
    except (json.JSONDecodeError, RecursionError, ValueError):
        return _failed("completion_envelope_malformed")
    if not isinstance(payload, dict) or set(payload) != _ENVELOPE_KEYS:
        return _failed("completion_envelope_invalid")
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
        return _failed("completion_envelope_invalid")
    if schema_version != 2:
        return _failed("completion_schema_mismatch")
    if job_id != identity.job_id:
        return _failed("completion_job_mismatch")
    if attempt != identity.attempt:
        return _failed("completion_attempt_mismatch")
    if fencing_token != identity.fencing_token:
        return _failed("completion_fencing_token_mismatch")
    try:
        status = CompletionStatus(status_value)
    except ValueError:
        return _failed("completion_status_invalid")
    if len(evidence_summary.encode("utf-8")) > MAX_EVIDENCE_SUMMARY_BYTES:
        return _failed("completion_evidence_oversized")
    if not evidence_summary.strip():
        return _failed("completion_evidence_invalid")
    sanitized = sanitize(evidence_summary)
    if not isinstance(sanitized, str) or not sanitized:
        return _failed("completion_evidence_invalid")
    return CompletionResult(
        CompletionPolicy.STRUCTURED_V2,
        VerificationClass.VERIFIED,
        status,
        sanitized,
        None,
    )


class _DuplicateJsonKey(ValueError):
    pass


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
