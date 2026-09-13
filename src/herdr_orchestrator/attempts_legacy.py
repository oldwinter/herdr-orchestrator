from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from herdr_orchestrator.attempts import AttemptLedger, StoreError
from herdr_orchestrator.model import AttemptPhase, JobState


@dataclass(frozen=True, slots=True)
class _LegacyOperation:
    lease_until: object
    phase: str
    sequence: int
    kind: str
    error_code: object
    updated_at: float
    finished_at: float | None
    clear_outcome: bool


def migrate_v4_to_v5(
    connection: sqlite3.Connection,
    add_column: Callable[[sqlite3.Connection, str, str, str], None],
) -> None:
    add_column(connection, "jobs", "current_attempt_id", "INTEGER")
    for column, declaration in (
        ("attempt_id", "INTEGER"),
        ("fencing_token", "TEXT"),
        ("operation_token", "TEXT"),
        ("operation_sequence", "INTEGER"),
        ("event_kind", "TEXT"),
        ("is_stale", "INTEGER NOT NULL DEFAULT 0"),
    ):
        add_column(connection, "receipts", column, declaration)
    AttemptLedger.create_schema(connection)
    _backfill(connection)
    connection.execute("UPDATE schema_meta SET version = 5")


def _backfill(connection: sqlite3.Connection) -> None:
    for job in connection.execute("SELECT * FROM jobs ORDER BY id").fetchall():
        numbers = {
            int(row["attempt"])
            for row in connection.execute(
                "SELECT DISTINCT attempt FROM receipts WHERE job_id = ?",
                (job["id"],),
            ).fetchall()
            if int(row["attempt"]) > 0
        }
        current_number = int(job["attempts"])
        if current_number > 0:
            numbers.add(current_number)
        for number in sorted(numbers):
            _backfill_one(connection, job, number)
        if current_number > 0:
            current = connection.execute(
                "SELECT id FROM job_attempts WHERE job_id = ? AND attempt = ?",
                (job["id"], current_number),
            ).fetchone()
            if current is not None:
                connection.execute(
                    "UPDATE jobs SET current_attempt_id = ? WHERE id = ?",
                    (current["id"], job["id"]),
                )


def _backfill_one(
    connection: sqlite3.Connection,
    job: sqlite3.Row,
    number: int,
) -> None:
    receipts = connection.execute(
        "SELECT * FROM receipts WHERE job_id = ? AND attempt = ? ORDER BY id",
        (job["id"], number),
    ).fetchall()
    latest = receipts[-1] if receipts else None
    current, correlation, fence = _legacy_identity(connection, job, number, latest)
    blocked_resume = bool(
        current and job["state"] == JobState.BLOCKED.value and job["lease_until"] is not None
    )
    operation_token = str(
        job["correlation_id"]
        if blocked_resume and job["correlation_id"]
        else (
            f"legacy-resume:{job['id']}:{number}:{len(receipts)}"
            if blocked_resume
            else correlation or fence
        )
    )
    _insert_legacy_attempt(
        connection,
        job,
        number,
        latest,
        receipts,
        current=current,
        fence=fence,
        operation_token=operation_token,
    )
    attempt = connection.execute(
        "SELECT id, fencing_token FROM job_attempts WHERE job_id = ? AND attempt = ?",
        (job["id"], number),
    ).fetchone()
    if attempt is None:
        raise StoreError("attempt_backfill_failed")
    _link_legacy_receipts(connection, receipts, attempt, fallback_operation_token=fence)


def _legacy_identity(
    connection: sqlite3.Connection,
    job: sqlite3.Row,
    number: int,
    latest: sqlite3.Row | None,
) -> tuple[bool, object, str]:
    current = number == int(job["attempts"])
    correlation = latest["correlation_id"] if latest is not None else None
    if correlation is None and current:
        correlation = job["correlation_id"]
    fence = str(correlation or f"legacy:{job['id']}:{number}")
    conflict = connection.execute(
        "SELECT job_id, attempt FROM job_attempts WHERE fencing_token = ?",
        (fence,),
    ).fetchone()
    if conflict is not None and (int(conflict["job_id"]), int(conflict["attempt"])) != (
        int(job["id"]),
        number,
    ):
        fence = f"legacy:{job['id']}:{number}"
    return current, correlation, fence


def _insert_legacy_attempt(
    connection: sqlite3.Connection,
    job: sqlite3.Row,
    number: int,
    latest: sqlite3.Row | None,
    receipts: list[sqlite3.Row],
    *,
    current: bool,
    fence: str,
    operation_token: str,
) -> None:
    operation = _legacy_operation(job, latest, receipts, current=current)
    agent_state, member_reused, agent_settled, task_verified, error_summary = (
        _legacy_outcome_evidence(operation, latest, job)
    )
    connection.execute(
        """
        INSERT INTO job_attempts(
            job_id, attempt, fencing_token, lease_owner, lease_until,
            selected_harness, agent_name, pane_id, herdr_workspace_id,
            execution_path, phase, operation_token, operation_sequence,
            operation_kind, agent_state, member_reused, agent_settled,
            task_verified, error_code, error_summary, created_at, updated_at,
            finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, attempt) DO NOTHING
        """,
        (
            job["id"],
            number,
            fence,
            f"legacy-owner:{job['id']}:{number}",
            operation.lease_until,
            job["harness"],
            str(_coalesce(latest or job, job, "agent_name") or "unknown"),
            _coalesce(latest or job, None, "pane_id"),
            _coalesce(latest or job, job, "herdr_workspace_id"),
            _coalesce(latest or job, job, "execution_path"),
            operation.phase,
            operation_token,
            operation.sequence,
            operation.kind,
            agent_state,
            member_reused,
            agent_settled,
            task_verified,
            operation.error_code,
            error_summary,
            float(job["created_at"]),
            operation.updated_at,
            operation.finished_at,
        ),
    )


def _legacy_operation(
    job: sqlite3.Row,
    latest: sqlite3.Row | None,
    receipts: list[sqlite3.Row],
    *,
    current: bool,
) -> _LegacyOperation:
    running = current and job["state"] == JobState.RUNNING.value
    blocked_resume = bool(
        current and job["state"] == JobState.BLOCKED.value and job["lease_until"] is not None
    )
    active = running or blocked_resume
    error_code = None if blocked_resume else _coalesce(latest or job, job, "error_code")
    phase = AttemptPhase.OUTCOME_COMMITTED.value
    if active:
        phase = (
            AttemptPhase.RUNTIME_ACQUIRED.value if blocked_resume else AttemptPhase.CLAIMED.value
        )
    elif error_code == "lease_expired":
        phase = AttemptPhase.ABANDONED.value
    updated_at = float(latest["observed_at"] if latest is not None else job["updated_at"])
    sequence = len(receipts) if blocked_resume else max(0, len(receipts) - 1)
    kind = "resume" if blocked_resume or sequence > 0 else "dispatch"
    return _LegacyOperation(
        job["lease_until"] if active else None,
        phase,
        sequence,
        kind,
        error_code,
        updated_at,
        None if active else updated_at,
        blocked_resume,
    )


def _legacy_outcome_evidence(
    operation: _LegacyOperation,
    latest: sqlite3.Row | None,
    job: sqlite3.Row,
) -> tuple[object, object, object, object, object]:
    if operation.clear_outcome:
        return None, None, None, None, None
    source = latest or job
    return (
        _coalesce(source, None, "agent_state"),
        _coalesce(source, None, "member_reused"),
        _coalesce(source, job, "agent_settled"),
        _coalesce(source, job, "task_verified"),
        _coalesce(source, job, "error_summary"),
    )


def _link_legacy_receipts(
    connection: sqlite3.Connection,
    receipts: list[sqlite3.Row],
    attempt: sqlite3.Row,
    *,
    fallback_operation_token: str,
) -> None:
    for operation_sequence, receipt in enumerate(receipts):
        connection.execute(
            """
            UPDATE receipts
            SET attempt_id = ?, fencing_token = ?, operation_token = ?,
                operation_sequence = ?, event_kind = ?, is_stale = 0
            WHERE id = ?
            """,
            (
                attempt["id"],
                attempt["fencing_token"],
                str(receipt["correlation_id"] or fallback_operation_token),
                operation_sequence,
                (
                    AttemptPhase.ABANDONED.value
                    if receipt["error_code"] == "lease_expired"
                    else AttemptPhase.OUTCOME_COMMITTED.value
                ),
                receipt["id"],
            ),
        )


def _coalesce(row: sqlite3.Row, fallback: sqlite3.Row | None, key: str) -> object:
    try:
        value = row[key]
    except (IndexError, KeyError):
        value = None
    if value is not None or fallback is None:
        return value
    try:
        return fallback[key]
    except (IndexError, KeyError):
        return None
