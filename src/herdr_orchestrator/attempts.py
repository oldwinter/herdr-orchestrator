from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    AttemptRuntime,
    ClaimedJob,
    DispatchOutcome,
    Harness,
    JobState,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
)
from herdr_orchestrator.observability import sanitize


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _NormalizedOutcome:
    state: JobState
    available_at: float
    observed_at: float
    error_code: str | None
    error_summary: str | None
    agent_settled: bool
    task_verified: bool | None
    correlation_id: str


@dataclass(frozen=True, slots=True)
class _ResumeLease:
    operation_token: str
    operation_sequence: int
    lease_owner: str
    lease_until: float
    correlation_id: str
    phase: AttemptPhase
    recovery: bool
    runtime: AttemptRuntime


_SETTLED_AGENT_STATES = frozenset({AgentState.IDLE, AgentState.DONE})
_PHASE_PREDECESSORS: dict[AttemptPhase, frozenset[AttemptPhase]] = {
    AttemptPhase.RUNTIME_ACQUIRED: frozenset(
        {AttemptPhase.CLAIMED, AttemptPhase.OUTCOME_COMMITTED}
    ),
    AttemptPhase.PROMPT_ACCEPTED: frozenset({AttemptPhase.RUNTIME_ACQUIRED}),
    AttemptPhase.SETTLED: frozenset({AttemptPhase.PROMPT_ACCEPTED}),
    AttemptPhase.RECEIPT_OBSERVED: frozenset({AttemptPhase.SETTLED}),
}


def _nullable_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _bounded_error_summary(value: str | None) -> str | None:
    if value is None:
        return None
    summary = str(sanitize(value))
    return summary or None


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


def _optional_text(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    return str(value) if value is not None else None


def _runtime(row: sqlite3.Row) -> AttemptRuntime:
    def text(key: str) -> str | None:
        value = row[key]
        return str(value) if value is not None else None

    def integer(key: str) -> int | None:
        value = row[key]
        return int(value) if value is not None else None

    return AttemptRuntime(
        agent_name=str(row["agent_name"]),
        pane_id=text("pane_id"),
        herdr_workspace_id=text("herdr_workspace_id"),
        execution_path=text("execution_path"),
        agent_session_id=text("agent_session_id"),
        prompt_baseline_sequence=integer("prompt_baseline_sequence"),
        prompt_accepted_sequence=integer("prompt_accepted_sequence"),
        state_change_sequence=integer("last_state_change_sequence"),
        phase=AttemptPhase(str(row["phase"])),
    )


def _insert_receipt(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    attempt: int,
    state: JobState,
    agent_name: str,
    agent_state: AgentState,
    member_reused: bool,
    pane_id: str | None,
    error_code: str | None,
    placement: str | None,
    execution_path: str | None,
    herdr_workspace_id: str | None,
    agent_settled: bool | None,
    task_verified: bool | None,
    error_summary: str | None,
    correlation_id: str | None,
    observed_at: float,
    attempt_id: int | None,
    fencing_token: str | None,
    operation_token: str | None,
    operation_sequence: int | None,
    event_kind: str,
    is_stale: bool = False,
) -> None:
    connection.execute(
        """
        INSERT INTO receipts(
            job_id, attempt, state, agent_name, agent_state, member_reused,
            pane_id, error_code, placement, execution_path, herdr_workspace_id,
            agent_settled, task_verified, error_summary, correlation_id,
            observed_at, attempt_id, fencing_token, operation_token,
            operation_sequence, event_kind, is_stale
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            attempt,
            state.value,
            agent_name,
            agent_state.value,
            int(member_reused),
            pane_id,
            error_code,
            placement,
            execution_path,
            herdr_workspace_id,
            int(agent_settled) if agent_settled is not None else None,
            int(task_verified) if task_verified is not None else None,
            error_summary,
            correlation_id,
            observed_at,
            attempt_id,
            fencing_token,
            operation_token,
            operation_sequence,
            event_kind,
            int(is_stale),
        ),
    )


def _agent_is_settled(outcome: DispatchOutcome) -> bool:
    if outcome.agent_settled is not None:
        return outcome.agent_settled
    return outcome.state in _SETTLED_AGENT_STATES


def _effective_error(job: ClaimedJob, outcome: DispatchOutcome) -> str | None:
    if outcome.error_code is not None:
        return outcome.error_code
    if job.receipt is not None and outcome.task_verified is not True:
        return "task_receipt_missing"
    if outcome.task_verified is False:
        return "task_receipt_invalid"
    if outcome.state in _SETTLED_AGENT_STATES and not _agent_is_settled(outcome):
        return "agent_not_settled"
    if outcome.state in {AgentState.WORKING, AgentState.UNKNOWN}:
        return "agent_not_settled"
    return None


def _normalize(
    job: ClaimedJob,
    outcome: DispatchOutcome,
    *,
    resume: bool,
    now: float,
) -> _NormalizedOutcome:
    error_code = _effective_error(job, outcome)
    if resume:
        state = (
            JobState.SUCCEEDED
            if error_code is None and outcome.state in _SETTLED_AGENT_STATES
            else JobState.BLOCKED
        )
        if error_code is None and outcome.state is AgentState.BLOCKED:
            error_code = "agent_blocked"
        elif error_code is None and outcome.state in {AgentState.WORKING, AgentState.UNKNOWN}:
            error_code = "agent_not_settled"
        available_at = now
    else:
        if error_code is None and outcome.state in _SETTLED_AGENT_STATES:
            state = JobState.SUCCEEDED
        elif error_code == "unsafe_turn_adoption" or outcome.state is AgentState.BLOCKED:
            state = JobState.BLOCKED
        elif job.attempt < job.max_attempts:
            state = JobState.PENDING
        else:
            state = JobState.FAILED
        available_at = (
            now + min(60, 2 ** max(0, job.attempt - 1)) if state is JobState.PENDING else now
        )
    return _NormalizedOutcome(
        state=state,
        available_at=available_at,
        observed_at=now,
        error_code=error_code,
        error_summary=_bounded_error_summary(outcome.error_summary),
        agent_settled=_agent_is_settled(outcome),
        task_verified=outcome.task_verified,
        correlation_id=outcome.correlation_id or job.correlation_id,
    )


class AttemptLedger:
    @staticmethod
    def create_schema(connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS job_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id),
                attempt INTEGER NOT NULL,
                fencing_token TEXT NOT NULL UNIQUE,
                lease_owner TEXT NOT NULL,
                lease_until REAL,
                selected_harness TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                pane_id TEXT,
                herdr_workspace_id TEXT,
                execution_path TEXT,
                agent_session_id TEXT,
                phase TEXT NOT NULL,
                prompt_baseline_sequence INTEGER,
                prompt_accepted_sequence INTEGER,
                last_state_change_sequence INTEGER,
                operation_token TEXT NOT NULL,
                operation_sequence INTEGER NOT NULL DEFAULT 0,
                operation_kind TEXT NOT NULL DEFAULT 'dispatch',
                agent_state TEXT,
                member_reused INTEGER,
                agent_settled INTEGER,
                task_verified INTEGER,
                error_code TEXT,
                error_summary TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                finished_at REAL,
                UNIQUE(job_id, attempt)
            )
            """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS job_attempts_active
            ON job_attempts(job_id, lease_until, phase)
            """)

    @staticmethod
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
        AttemptLedger._backfill(connection)
        connection.execute("UPDATE schema_meta SET version = 5")

    @staticmethod
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
                AttemptLedger._backfill_one(connection, job, number)
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

    @staticmethod
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
        current, correlation, fence = AttemptLedger._legacy_identity(
            connection, job, number, latest
        )
        operation_token = str(correlation or fence)
        AttemptLedger._insert_legacy_attempt(
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
        AttemptLedger._link_legacy_receipts(
            connection, receipts, attempt, operation_token=operation_token
        )

    @staticmethod
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

    @staticmethod
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
        running = current and job["state"] == JobState.RUNNING.value
        latest_error = _coalesce(latest or job, job, "error_code")
        phase = (
            AttemptPhase.CLAIMED.value
            if running
            else (
                AttemptPhase.ABANDONED.value
                if latest_error == "lease_expired"
                else AttemptPhase.OUTCOME_COMMITTED.value
            )
        )
        updated_at = float(latest["observed_at"] if latest is not None else job["updated_at"])
        sequence = max(0, len(receipts) - 1)
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
                job["lease_until"] if running else None,
                job["harness"],
                str(_coalesce(latest or job, job, "agent_name") or "unknown"),
                _coalesce(latest or job, None, "pane_id"),
                _coalesce(latest or job, job, "herdr_workspace_id"),
                _coalesce(latest or job, job, "execution_path"),
                phase,
                operation_token,
                sequence,
                "dispatch" if sequence == 0 else "resume",
                _coalesce(latest or job, None, "agent_state"),
                _coalesce(latest or job, None, "member_reused"),
                _coalesce(latest or job, job, "agent_settled"),
                _coalesce(latest or job, job, "task_verified"),
                latest_error,
                _coalesce(latest or job, job, "error_summary"),
                float(job["created_at"]),
                updated_at,
                None if running else updated_at,
            ),
        )

    @staticmethod
    def _link_legacy_receipts(
        connection: sqlite3.Connection,
        receipts: list[sqlite3.Row],
        attempt: sqlite3.Row,
        *,
        operation_token: str,
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
                    str(receipt["correlation_id"] or operation_token),
                    operation_sequence,
                    (
                        AttemptPhase.ABANDONED.value
                        if receipt["error_code"] == "lease_expired"
                        else AttemptPhase.OUTCOME_COMMITTED.value
                    ),
                    receipt["id"],
                ),
            )

    @staticmethod
    def receipt_from_row(row: sqlite3.Row) -> TaskReceipt | None:
        if row["receipt_kind"] is None or row["receipt_value"] is None:
            return None
        return TaskReceipt(ReceiptKind(str(row["receipt_kind"])), str(row["receipt_value"]))

    @staticmethod
    def create_claim(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        agent_name: str,
        now: float,
        lease_until: float,
    ) -> ClaimedJob:
        number = int(row["attempts"]) + 1
        correlation = uuid.uuid4().hex
        fence = uuid.uuid4().hex
        owner = uuid.uuid4().hex
        operation = uuid.uuid4().hex
        cursor = connection.execute(
            """
            INSERT INTO job_attempts(
                job_id, attempt, fencing_token, lease_owner, lease_until,
                selected_harness, agent_name, phase, operation_token,
                operation_sequence, operation_kind, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'dispatch', ?, ?)
            """,
            (
                row["id"],
                number,
                fence,
                owner,
                lease_until,
                row["harness"],
                agent_name,
                AttemptPhase.CLAIMED.value,
                operation,
                now,
                now,
            ),
        )
        if cursor.lastrowid is None:
            raise StoreError("attempt_id_missing")
        attempt_id = int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE jobs
            SET state = ?, attempts = ?, lease_until = ?, agent_name = ?,
                error_code = NULL, execution_path = NULL, herdr_workspace_id = NULL,
                agent_settled = NULL, task_verified = NULL, error_summary = NULL,
                correlation_id = ?, current_attempt_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                JobState.RUNNING.value,
                number,
                lease_until,
                agent_name,
                correlation,
                attempt_id,
                now,
                row["id"],
            ),
        )
        return ClaimedJob(
            int(row["id"]),
            str(row["workflow"]),
            str(row["title"]),
            Harness(str(row["harness"])),
            str(row["prompt"]),
            str(row["dedupe_key"]),
            number,
            int(row["max_attempts"]),
            agent_name,
            PlacementTarget(str(row["placement"])),
            AttemptLedger.receipt_from_row(row),
            correlation,
            attempt_id,
            fence,
            owner,
            lease_until,
            operation,
        )

    @staticmethod
    def reclaim(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: float,
        lease_until: float,
    ) -> ClaimedJob:
        attempt_id = row["current_attempt_id"]
        if attempt_id is None:
            raise StoreError("current_attempt_missing")
        attempt = connection.execute(
            "SELECT * FROM job_attempts WHERE id = ? AND job_id = ?",
            (attempt_id, row["id"]),
        ).fetchone()
        if attempt is None:
            raise StoreError("current_attempt_missing")
        owner = uuid.uuid4().hex
        correlation = uuid.uuid4().hex
        attempt_cursor = connection.execute(
            """
            UPDATE job_attempts SET lease_owner = ?, lease_until = ?, updated_at = ?
            WHERE id = ? AND job_id = ? AND fencing_token = ? AND lease_until <= ?
            """,
            (owner, lease_until, now, attempt["id"], row["id"], attempt["fencing_token"], now),
        )
        job_cursor = connection.execute(
            """
            UPDATE jobs SET lease_until = ?, correlation_id = ?, updated_at = ?
            WHERE id = ? AND state = ? AND current_attempt_id = ? AND lease_until <= ?
            """,
            (
                lease_until,
                correlation,
                now,
                row["id"],
                JobState.RUNNING.value,
                attempt["id"],
                now,
            ),
        )
        if attempt_cursor.rowcount != 1 or job_cursor.rowcount != 1:
            raise StoreError("job_lease_lost")
        return ClaimedJob(
            int(row["id"]),
            str(row["workflow"]),
            str(row["title"]),
            Harness(str(row["harness"])),
            str(row["prompt"]),
            str(row["dedupe_key"]),
            int(attempt["attempt"]),
            int(row["max_attempts"]),
            str(attempt["agent_name"]),
            PlacementTarget(str(row["placement"])),
            AttemptLedger.receipt_from_row(row),
            correlation,
            int(attempt["id"]),
            str(attempt["fencing_token"]),
            owner,
            lease_until,
            str(attempt["operation_token"]),
            int(attempt["operation_sequence"]),
            AttemptPhase(str(attempt["phase"])),
            True,
            _runtime(attempt),
        )

    @staticmethod
    def record_progress(
        connection: sqlite3.Connection,
        job: ClaimedJob,
        progress: AttemptProgress,
        *,
        now: float,
    ) -> bool:
        row = AttemptLedger._owned_row(connection, job)
        if not AttemptLedger._owner_matches(row, job, now):
            AttemptLedger._stale_progress(connection, row, job, progress, now)
            return False
        assert row is not None
        current = AttemptPhase(str(row["phase"]))
        predecessors = _PHASE_PREDECESSORS.get(progress.phase, frozenset())
        if current is not progress.phase and current not in predecessors:
            raise StoreError("attempt_phase_invalid")
        cursor = connection.execute(
            """
            UPDATE job_attempts
            SET phase = ?, agent_name = ?, pane_id = COALESCE(?, pane_id),
                herdr_workspace_id = COALESCE(?, herdr_workspace_id),
                execution_path = COALESCE(?, execution_path),
                agent_session_id = COALESCE(?, agent_session_id),
                prompt_baseline_sequence = COALESCE(?, prompt_baseline_sequence),
                prompt_accepted_sequence = COALESCE(?, prompt_accepted_sequence),
                last_state_change_sequence = COALESCE(?, last_state_change_sequence),
                agent_state = COALESCE(?, agent_state),
                member_reused = COALESCE(?, member_reused),
                agent_settled = COALESCE(?, agent_settled),
                task_verified = COALESCE(?, task_verified),
                error_code = COALESCE(?, error_code),
                error_summary = COALESCE(?, error_summary), updated_at = ?
            WHERE id = ? AND job_id = ? AND fencing_token = ?
              AND lease_owner = ? AND operation_token = ? AND lease_until > ?
            """,
            (
                progress.phase.value,
                progress.agent_name,
                progress.pane_id,
                progress.herdr_workspace_id,
                progress.execution_path,
                progress.agent_session_id,
                progress.prompt_baseline_sequence,
                progress.prompt_accepted_sequence,
                progress.state_change_sequence,
                progress.agent_state.value if progress.agent_state is not None else None,
                int(progress.member_reused) if progress.member_reused is not None else None,
                int(progress.agent_settled) if progress.agent_settled is not None else None,
                int(progress.task_verified) if progress.task_verified is not None else None,
                progress.error_code,
                _bounded_error_summary(progress.error_summary),
                now,
                job.attempt_id,
                job.job_id,
                job.fencing_token,
                job.lease_owner,
                job.operation_token,
                now,
            ),
        )
        if cursor.rowcount != 1:
            raise StoreError("job_lease_lost")
        connection.execute(
            """
            UPDATE jobs
            SET agent_name = ?, execution_path = COALESCE(?, execution_path),
                herdr_workspace_id = COALESCE(?, herdr_workspace_id), updated_at = ?
            WHERE id = ? AND current_attempt_id = ?
            """,
            (
                progress.agent_name,
                progress.execution_path,
                progress.herdr_workspace_id,
                now,
                job.job_id,
                job.attempt_id,
            ),
        )
        return True

    @staticmethod
    def record_outcome(
        connection: sqlite3.Connection,
        job: ClaimedJob,
        outcome: DispatchOutcome,
        *,
        resume: bool,
        now: float,
    ) -> tuple[JobState, bool]:
        if not (job.attempt_id and job.fencing_token and job.lease_owner and job.operation_token):
            raise StoreError("job_lease_lost")
        normalized = _normalize(job, outcome, resume=resume, now=now)
        row = AttemptLedger._owned_row(connection, job)
        expected_state = JobState.BLOCKED if resume else JobState.RUNNING
        if not AttemptLedger._outcome_owner_matches(
            row, job, normalized, expected_state=expected_state, now=now
        ):
            AttemptLedger._stale_outcome(connection, row, job, outcome, normalized)
            return normalized.state, False
        assert row is not None
        normalized, phase = AttemptLedger._classify_outcome(
            normalized,
            AttemptPhase(str(row["phase"])),
            outcome,
            resume=resume,
            now=now,
        )
        AttemptLedger._commit_attempt_outcome(
            connection, job, outcome, normalized, phase=phase, now=now
        )
        AttemptLedger._commit_job_outcome(
            connection,
            job,
            outcome,
            normalized,
            expected_state=expected_state,
            now=now,
        )
        AttemptLedger._append_outcome_receipt(
            connection, job, outcome, normalized, phase=phase, now=now
        )
        return normalized.state, True

    @staticmethod
    def _outcome_owner_matches(
        row: sqlite3.Row | None,
        job: ClaimedJob,
        normalized: _NormalizedOutcome,
        *,
        expected_state: JobState,
        now: float,
    ) -> bool:
        return bool(
            AttemptLedger._owner_matches(row, job, now)
            and row is not None
            and row["job_state"] == expected_state.value
            and row["job_correlation_id"] == job.correlation_id
            and (not normalized.correlation_id or normalized.correlation_id == job.correlation_id)
        )

    @staticmethod
    def _classify_outcome(
        normalized: _NormalizedOutcome,
        current_phase: AttemptPhase,
        outcome: DispatchOutcome,
        *,
        resume: bool,
        now: float,
    ) -> tuple[_NormalizedOutcome, AttemptPhase]:
        accepted_unsettled = bool(
            not resume
            and current_phase
            in {
                AttemptPhase.PROMPT_ACCEPTED,
                AttemptPhase.SETTLED,
                AttemptPhase.RECEIPT_OBSERVED,
            }
            and outcome.state in {AgentState.WORKING, AgentState.UNKNOWN}
        )
        needs_attention = accepted_unsettled or normalized.error_code in {
            "unsafe_turn_adoption",
            "task_receipt_recovery_unverified",
        }
        if needs_attention:
            return (
                replace(normalized, state=JobState.BLOCKED, available_at=now),
                AttemptPhase.ATTENTION,
            )
        abandoned = bool(
            (
                normalized.error_code is not None
                and current_phase in {AttemptPhase.CLAIMED, AttemptPhase.RUNTIME_ACQUIRED}
                and outcome.state is not AgentState.BLOCKED
            )
            or normalized.state is JobState.PENDING
        )
        return normalized, (AttemptPhase.ABANDONED if abandoned else AttemptPhase.OUTCOME_COMMITTED)

    @staticmethod
    def _commit_attempt_outcome(
        connection: sqlite3.Connection,
        job: ClaimedJob,
        outcome: DispatchOutcome,
        normalized: _NormalizedOutcome,
        *,
        phase: AttemptPhase,
        now: float,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE job_attempts
            SET lease_until = NULL, phase = ?, agent_name = ?,
                pane_id = COALESCE(?, pane_id),
                herdr_workspace_id = COALESCE(?, herdr_workspace_id),
                execution_path = COALESCE(?, execution_path), agent_state = ?,
                member_reused = ?, agent_settled = ?, task_verified = ?,
                error_code = ?, error_summary = ?, updated_at = ?, finished_at = ?
            WHERE id = ? AND job_id = ? AND fencing_token = ?
              AND lease_owner = ? AND operation_token = ? AND lease_until > ?
            """,
            (
                phase.value,
                outcome.agent_name,
                outcome.pane_id,
                outcome.herdr_workspace_id,
                outcome.execution_path,
                outcome.state.value,
                int(outcome.member_reused),
                int(normalized.agent_settled),
                int(normalized.task_verified) if normalized.task_verified is not None else None,
                normalized.error_code,
                normalized.error_summary,
                now,
                now,
                job.attempt_id,
                job.job_id,
                job.fencing_token,
                job.lease_owner,
                job.operation_token,
                now,
            ),
        )
        if cursor.rowcount != 1:
            raise StoreError("job_lease_lost")

    @staticmethod
    def _commit_job_outcome(
        connection: sqlite3.Connection,
        job: ClaimedJob,
        outcome: DispatchOutcome,
        normalized: _NormalizedOutcome,
        *,
        expected_state: JobState,
        now: float,
    ) -> None:
        job_cursor = connection.execute(
            """
            UPDATE jobs
            SET state = ?, available_at = ?, lease_until = NULL, agent_name = ?,
                error_code = ?, execution_path = ?, herdr_workspace_id = ?,
                agent_settled = ?, task_verified = ?, error_summary = ?,
                correlation_id = ?, updated_at = ?
            WHERE id = ? AND current_attempt_id = ? AND state = ? AND correlation_id = ?
            """,
            (
                normalized.state.value,
                normalized.available_at,
                outcome.agent_name,
                normalized.error_code,
                outcome.execution_path,
                outcome.herdr_workspace_id,
                int(normalized.agent_settled),
                int(normalized.task_verified) if normalized.task_verified is not None else None,
                normalized.error_summary,
                job.correlation_id,
                now,
                job.job_id,
                job.attempt_id,
                expected_state.value,
                job.correlation_id,
            ),
        )
        if job_cursor.rowcount != 1:
            raise StoreError("job_lease_lost")

    @staticmethod
    def _append_outcome_receipt(
        connection: sqlite3.Connection,
        job: ClaimedJob,
        outcome: DispatchOutcome,
        normalized: _NormalizedOutcome,
        *,
        phase: AttemptPhase,
        now: float,
    ) -> None:
        _insert_receipt(
            connection,
            job_id=job.job_id,
            attempt=job.attempt,
            state=normalized.state,
            agent_name=outcome.agent_name,
            agent_state=outcome.state,
            member_reused=outcome.member_reused,
            pane_id=outcome.pane_id,
            error_code=normalized.error_code,
            placement=outcome.placement.value if outcome.placement else job.placement.value,
            execution_path=outcome.execution_path,
            herdr_workspace_id=outcome.herdr_workspace_id,
            agent_settled=normalized.agent_settled,
            task_verified=normalized.task_verified,
            error_summary=normalized.error_summary,
            correlation_id=job.correlation_id,
            observed_at=now,
            attempt_id=job.attempt_id,
            fencing_token=job.fencing_token,
            operation_token=job.operation_token,
            operation_sequence=job.operation_sequence,
            event_kind=phase.value,
        )

    @staticmethod
    def claim_resume(
        connection: sqlite3.Connection,
        workflow: str,
        job_id: int,
        *,
        lease_seconds: int,
        now: float,
    ) -> tuple[ClaimedJob, str]:
        job, attempt, phase, pane_id = AttemptLedger._resume_candidate(
            connection, workflow, job_id, now=now
        )
        recovery = AttemptLedger._can_recover_resume(attempt, phase)
        if not recovery and attempt["operation_kind"] == "resume" and phase is AttemptPhase.CLAIMED:
            AttemptLedger._append_expired_resume(connection, job, attempt, pane_id, now=now)
        lease = AttemptLedger._acquire_resume_attempt(
            connection,
            job,
            attempt,
            phase,
            pane_id,
            recovery=recovery,
            lease_seconds=lease_seconds,
            now=now,
        )
        AttemptLedger._lease_blocked_job(connection, job, attempt, lease, now=now)
        return AttemptLedger._resume_claim(job, attempt, pane_id, lease), pane_id

    @staticmethod
    def _resume_candidate(
        connection: sqlite3.Connection,
        workflow: str,
        job_id: int,
        *,
        now: float,
    ) -> tuple[sqlite3.Row, sqlite3.Row, AttemptPhase, str]:
        job = connection.execute(
            "SELECT * FROM jobs WHERE workflow = ? AND id = ?",
            (workflow, job_id),
        ).fetchone()
        if job is None:
            raise StoreError("job_not_found")
        if job["state"] != JobState.BLOCKED.value:
            raise StoreError("job_not_resumable")
        if job["lease_until"] is not None and float(job["lease_until"]) > now:
            raise StoreError("job_resume_in_progress")
        attempt_id = job["current_attempt_id"]
        if attempt_id is None:
            raise StoreError("current_attempt_missing")
        attempt = connection.execute(
            "SELECT * FROM job_attempts WHERE id = ? AND job_id = ?",
            (attempt_id, job_id),
        ).fetchone()
        if attempt is None:
            raise StoreError("current_attempt_missing")
        phase = AttemptPhase(str(attempt["phase"]))
        if phase is AttemptPhase.ATTENTION:
            raise StoreError("job_not_resumable")
        pane_id = attempt["pane_id"]
        if not isinstance(attempt["agent_name"], str) or not attempt["agent_name"]:
            raise StoreError("blocked_agent_missing")
        if not isinstance(pane_id, str) or not pane_id:
            raise StoreError("blocked_pane_missing")
        if not isinstance(job["placement"], str) or not job["placement"]:
            raise StoreError("blocked_placement_missing")
        return job, attempt, phase, pane_id

    @staticmethod
    def _can_recover_resume(attempt: sqlite3.Row, phase: AttemptPhase) -> bool:
        return bool(
            attempt["operation_kind"] == "resume"
            and phase
            in {
                AttemptPhase.RUNTIME_ACQUIRED,
                AttemptPhase.PROMPT_ACCEPTED,
                AttemptPhase.SETTLED,
                AttemptPhase.RECEIPT_OBSERVED,
            }
        )

    @staticmethod
    def _append_expired_resume(
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        attempt: sqlite3.Row,
        pane_id: str,
        *,
        now: float,
    ) -> None:
        _insert_receipt(
            connection,
            job_id=int(job["id"]),
            attempt=int(attempt["attempt"]),
            state=JobState.BLOCKED,
            agent_name=str(attempt["agent_name"]),
            agent_state=AgentState.UNKNOWN,
            member_reused=True,
            pane_id=pane_id,
            error_code="resume_lease_expired_unaccepted",
            placement=str(job["placement"]),
            execution_path=_optional_text(attempt, "execution_path"),
            herdr_workspace_id=_optional_text(attempt, "herdr_workspace_id"),
            agent_settled=None,
            task_verified=None,
            error_summary=None,
            correlation_id=_optional_text(job, "correlation_id"),
            observed_at=now,
            attempt_id=int(attempt["id"]),
            fencing_token=str(attempt["fencing_token"]),
            operation_token=str(attempt["operation_token"]),
            operation_sequence=int(attempt["operation_sequence"]),
            event_kind=AttemptPhase.ABANDONED.value,
        )

    @staticmethod
    def _acquire_resume_attempt(
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        attempt: sqlite3.Row,
        phase: AttemptPhase,
        pane_id: str,
        *,
        recovery: bool,
        lease_seconds: int,
        now: float,
    ) -> _ResumeLease:
        operation = str(attempt["operation_token"]) if recovery else uuid.uuid4().hex
        sequence = int(attempt["operation_sequence"]) + (0 if recovery else 1)
        owner = uuid.uuid4().hex
        lease_until = now + lease_seconds
        correlation = uuid.uuid4().hex
        next_phase = phase if recovery else AttemptPhase.CLAIMED
        if recovery:
            cursor = connection.execute(
                """
                UPDATE job_attempts
                SET lease_owner = ?, lease_until = ?, updated_at = ?, finished_at = NULL
                WHERE id = ? AND job_id = ? AND fencing_token = ?
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (
                    owner,
                    lease_until,
                    now,
                    attempt["id"],
                    job["id"],
                    attempt["fencing_token"],
                    now,
                ),
            )
            runtime = _runtime(attempt)
        else:
            cursor = AttemptLedger._start_resume_operation(
                connection,
                job,
                attempt,
                operation=operation,
                sequence=sequence,
                owner=owner,
                lease_until=lease_until,
                now=now,
            )
            runtime = AttemptRuntime(
                str(attempt["agent_name"]),
                pane_id,
                _optional_text(attempt, "herdr_workspace_id"),
                _optional_text(attempt, "execution_path"),
                None,
                None,
                None,
                None,
            )
        if cursor.rowcount != 1:
            raise StoreError("job_resume_in_progress")
        return _ResumeLease(
            operation,
            sequence,
            owner,
            lease_until,
            correlation,
            next_phase,
            recovery,
            runtime,
        )

    @staticmethod
    def _start_resume_operation(
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        attempt: sqlite3.Row,
        *,
        operation: str,
        sequence: int,
        owner: str,
        lease_until: float,
        now: float,
    ) -> sqlite3.Cursor:
        return connection.execute(
            """
            UPDATE job_attempts
            SET lease_owner = ?, lease_until = ?, operation_token = ?,
                operation_sequence = ?, operation_kind = 'resume', phase = ?,
                agent_session_id = NULL, prompt_baseline_sequence = NULL,
                prompt_accepted_sequence = NULL, last_state_change_sequence = NULL,
                agent_state = NULL, member_reused = NULL, agent_settled = NULL,
                task_verified = NULL, error_code = NULL, error_summary = NULL,
                finished_at = NULL, updated_at = ?
            WHERE id = ? AND job_id = ? AND fencing_token = ?
              AND (lease_until IS NULL OR lease_until <= ?)
            """,
            (
                owner,
                lease_until,
                operation,
                sequence,
                AttemptPhase.CLAIMED.value,
                now,
                attempt["id"],
                job["id"],
                attempt["fencing_token"],
                now,
            ),
        )

    @staticmethod
    def _lease_blocked_job(
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        attempt: sqlite3.Row,
        lease: _ResumeLease,
        *,
        now: float,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE jobs SET lease_until = ?, correlation_id = ?, updated_at = ?
            WHERE id = ? AND state = ? AND current_attempt_id = ?
              AND (lease_until IS NULL OR lease_until <= ?)
            """,
            (
                lease.lease_until,
                lease.correlation_id,
                now,
                job["id"],
                JobState.BLOCKED.value,
                attempt["id"],
                now,
            ),
        )
        if cursor.rowcount != 1:
            raise StoreError("job_resume_in_progress")

    @staticmethod
    def _resume_claim(
        job: sqlite3.Row,
        attempt: sqlite3.Row,
        pane_id: str,
        lease: _ResumeLease,
    ) -> ClaimedJob:
        return ClaimedJob(
            int(job["id"]),
            str(job["workflow"]),
            str(job["title"]),
            Harness(str(job["harness"])),
            str(job["prompt"]),
            str(job["dedupe_key"]),
            int(attempt["attempt"]),
            int(job["max_attempts"]),
            str(attempt["agent_name"]),
            PlacementTarget(str(job["placement"])),
            AttemptLedger.receipt_from_row(job),
            lease.correlation_id,
            int(attempt["id"]),
            str(attempt["fencing_token"]),
            lease.lease_owner,
            lease.lease_until,
            lease.operation_token,
            lease.operation_sequence,
            lease.phase,
            lease.recovery,
            lease.runtime,
        )

    @staticmethod
    def _owned_row(
        connection: sqlite3.Connection,
        job: ClaimedJob,
    ) -> sqlite3.Row | None:
        row: sqlite3.Row | None = connection.execute(
            """
            SELECT attempts.*, jobs.state AS job_state,
                   jobs.current_attempt_id AS job_current_attempt_id,
                   jobs.correlation_id AS job_correlation_id
            FROM job_attempts AS attempts
            JOIN jobs ON jobs.id = attempts.job_id
            WHERE attempts.id = ? AND attempts.job_id = ?
            """,
            (job.attempt_id, job.job_id),
        ).fetchone()
        return row

    @staticmethod
    def _owner_matches(row: sqlite3.Row | None, job: ClaimedJob, now: float) -> bool:
        return bool(
            row is not None
            and row["job_current_attempt_id"] is not None
            and int(row["job_current_attempt_id"]) == job.attempt_id
            and int(row["attempt"]) == job.attempt
            and row["fencing_token"] == job.fencing_token
            and row["lease_owner"] == job.lease_owner
            and row["operation_token"] == job.operation_token
            and row["lease_until"] is not None
            and float(row["lease_until"]) > now
        )

    @staticmethod
    def _stale_progress(
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
        job: ClaimedJob,
        progress: AttemptProgress,
        now: float,
    ) -> None:
        state = JobState(str(row["job_state"])) if row is not None else JobState.RUNNING
        _insert_receipt(
            connection,
            job_id=job.job_id,
            attempt=job.attempt,
            state=state,
            agent_name=progress.agent_name,
            agent_state=progress.agent_state or AgentState.UNKNOWN,
            member_reused=bool(progress.member_reused),
            pane_id=progress.pane_id,
            error_code="job_lease_lost",
            placement=job.placement.value,
            execution_path=progress.execution_path,
            herdr_workspace_id=progress.herdr_workspace_id,
            agent_settled=progress.agent_settled,
            task_verified=progress.task_verified,
            error_summary=_bounded_error_summary(progress.error_summary),
            correlation_id=job.correlation_id or None,
            observed_at=now,
            attempt_id=job.attempt_id or None,
            fencing_token=job.fencing_token or None,
            operation_token=job.operation_token or None,
            operation_sequence=job.operation_sequence,
            event_kind=f"stale:{progress.phase.value}",
            is_stale=True,
        )

    @staticmethod
    def _stale_outcome(
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
        job: ClaimedJob,
        outcome: DispatchOutcome,
        normalized: _NormalizedOutcome,
    ) -> None:
        state = JobState(str(row["job_state"])) if row is not None else normalized.state
        _insert_receipt(
            connection,
            job_id=job.job_id,
            attempt=job.attempt,
            state=state,
            agent_name=outcome.agent_name,
            agent_state=outcome.state,
            member_reused=outcome.member_reused,
            pane_id=outcome.pane_id,
            error_code=normalized.error_code,
            placement=outcome.placement.value if outcome.placement else job.placement.value,
            execution_path=outcome.execution_path,
            herdr_workspace_id=outcome.herdr_workspace_id,
            agent_settled=normalized.agent_settled,
            task_verified=normalized.task_verified,
            error_summary=normalized.error_summary,
            correlation_id=normalized.correlation_id or job.correlation_id or None,
            observed_at=normalized.observed_at,
            attempt_id=job.attempt_id or None,
            fencing_token=job.fencing_token or None,
            operation_token=job.operation_token or None,
            operation_sequence=job.operation_sequence,
            event_kind="stale:outcome",
            is_stale=True,
        )
