from __future__ import annotations

import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from herdr_orchestrator.model import (
    AgentState,
    ClaimedJob,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
)
from herdr_orchestrator.observability import sanitize

SCHEMA_VERSION = 4


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


def _nullable_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _bounded_error_summary(value: str | None) -> str | None:
    if value is None:
        return None
    summary = str(sanitize(value))
    return summary or None


def _candidate_slot_names(
    row: sqlite3.Row,
    harness_value: str,
    placement_value: str,
    slot_names: Mapping[str, Sequence[str]] | None,
) -> tuple[str, ...]:
    slot_key = (
        f"{harness_value}:{placement_value}:{row['id']}"
        if placement_value == PlacementTarget.WORKTREE.value
        else f"{harness_value}:{placement_value}"
    )
    names = tuple(
        (slot_names.get(slot_key) or slot_names.get(harness_value) or ()) if slot_names else ()
    )
    return names or (f"ho-{harness_value}",)


def _receipt_from_row(row: sqlite3.Row) -> TaskReceipt | None:
    if row["receipt_kind"] is None or row["receipt_value"] is None:
        return None
    return TaskReceipt(
        ReceiptKind(str(row["receipt_kind"])),
        str(row["receipt_value"]),
    )


def _job_contract_matches(row: sqlite3.Row, job: NewJob) -> bool:
    return all(
        (
            row["title"] == job.title,
            row["harness"] == job.harness.value,
            row["prompt"] == job.prompt,
            row["placement"] == (job.placement.value if job.placement is not None else None),
            row["receipt_kind"] == (job.receipt.kind.value if job.receipt is not None else None),
            row["receipt_value"] == (job.receipt.value if job.receipt is not None else None),
        )
    )


def _partial_job_contract_matches(
    row: sqlite3.Row,
    *,
    title: str,
    prompt: str,
    harness: Harness | None,
    placement: PlacementTarget | None,
    receipt: TaskReceipt | None,
) -> bool:
    return all(
        (
            row["title"] == title,
            row["prompt"] == prompt,
            harness is None or row["harness"] == harness.value,
            placement is None or row["placement"] == placement.value,
            row["receipt_kind"] == (receipt.kind.value if receipt is not None else None),
            row["receipt_value"] == (receipt.value if receipt is not None else None),
        )
    )


def _insert_attempt_receipt(
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
) -> None:
    connection.execute(
        """
        INSERT INTO receipts(
            job_id, attempt, state, agent_name, agent_state,
            member_reused, pane_id, error_code, placement,
            execution_path, herdr_workspace_id, agent_settled,
            task_verified, error_summary, correlation_id, observed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            (int(agent_settled) if agent_settled is not None else None),
            (int(task_verified) if task_verified is not None else None),
            error_summary,
            correlation_id,
            observed_at,
        ),
    )


def _coalesce_row_value(
    row: sqlite3.Row,
    fallback: sqlite3.Row | None,
    key: str,
) -> object:
    try:
        value = row[key]
    except (IndexError, KeyError):
        value = None
    if value is not None:
        return value
    if fallback is None:
        return None
    try:
        return fallback[key]
    except (IndexError, KeyError):
        return None


def _record_expired_attempt(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    state: JobState,
    observed_at: float,
) -> None:
    job_id = int(row["id"])
    attempt = int(row["attempts"])
    existing = connection.execute(
        """
        SELECT 1 FROM receipts
        WHERE job_id = ? AND attempt = ? AND error_code = 'lease_expired'
        LIMIT 1
        """,
        (job_id, attempt),
    ).fetchone()
    if existing is not None:
        return
    latest = connection.execute(
        """
        SELECT agent_name, member_reused, pane_id, placement,
               execution_path, herdr_workspace_id, agent_settled,
               task_verified, error_summary, correlation_id
        FROM receipts
        WHERE job_id = ? AND attempt = ?
        ORDER BY id DESC LIMIT 1
        """,
        (job_id, attempt),
    ).fetchone()
    agent_value = _coalesce_row_value(row, latest, "agent_name")
    agent_name = str(agent_value or "unknown")
    member_reused_value = _coalesce_row_value(row, latest, "member_reused")
    member_reused = bool(_nullable_bool(member_reused_value))
    placement_value = _coalesce_row_value(row, latest, "placement")
    execution_path_value = _coalesce_row_value(row, latest, "execution_path")
    workspace_value = _coalesce_row_value(row, latest, "herdr_workspace_id")
    pane_value = _coalesce_row_value(row, latest, "pane_id")
    settled_value = _coalesce_row_value(row, latest, "agent_settled")
    verified_value = _coalesce_row_value(row, latest, "task_verified")
    summary_value = _coalesce_row_value(row, latest, "error_summary")
    correlation_value = _coalesce_row_value(row, latest, "correlation_id")
    _insert_attempt_receipt(
        connection,
        job_id=job_id,
        attempt=attempt,
        state=state,
        agent_name=agent_name,
        agent_state=AgentState.UNKNOWN,
        member_reused=member_reused,
        pane_id=str(pane_value) if pane_value is not None else None,
        error_code="lease_expired",
        placement=str(placement_value) if placement_value is not None else None,
        execution_path=(str(execution_path_value) if execution_path_value is not None else None),
        herdr_workspace_id=(str(workspace_value) if workspace_value is not None else None),
        agent_settled=_nullable_bool(settled_value),
        task_verified=_nullable_bool(verified_value),
        error_summary=(
            _bounded_error_summary(str(summary_value)) if summary_value is not None else None
        ),
        correlation_id=str(correlation_value) if correlation_value is not None else None,
        observed_at=observed_at,
    )


_SETTLED_AGENT_STATES = frozenset({AgentState.IDLE, AgentState.DONE})


def _agent_is_settled(outcome: DispatchOutcome) -> bool:
    return (
        outcome.agent_settled
        if outcome.agent_settled is not None
        else outcome.state in _SETTLED_AGENT_STATES
    )


def _effective_error_code(job: ClaimedJob, outcome: DispatchOutcome) -> str | None:
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


def _dispatch_state(job: ClaimedJob, outcome: DispatchOutcome, error_code: str | None) -> JobState:
    if error_code is None and outcome.state in _SETTLED_AGENT_STATES:
        return JobState.SUCCEEDED
    if outcome.state is AgentState.BLOCKED:
        return JobState.BLOCKED
    if job.attempt < job.max_attempts:
        return JobState.PENDING
    return JobState.FAILED


def _resume_error_code(outcome: DispatchOutcome) -> str | None:
    if outcome.state is AgentState.BLOCKED:
        return "agent_blocked"
    if outcome.state in {AgentState.WORKING, AgentState.UNKNOWN}:
        return "agent_not_settled"
    return None


def _normalize_outcome(
    job: ClaimedJob,
    outcome: DispatchOutcome,
    *,
    resume: bool,
) -> _NormalizedOutcome:
    now = time.time()
    error_code = _effective_error_code(job, outcome)
    if resume:
        state = (
            JobState.SUCCEEDED
            if error_code is None and outcome.state in _SETTLED_AGENT_STATES
            else JobState.BLOCKED
        )
        if error_code is None:
            error_code = _resume_error_code(outcome)
        available_at = now
    else:
        state = _dispatch_state(job, outcome, error_code)
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


def _validate_owned_attempt(
    row: sqlite3.Row | None,
    job: ClaimedJob,
    outcome_correlation: str,
    expected_state: JobState,
    now: float,
    *,
    require_fence: bool,
) -> str:
    if row is None:
        raise StoreError("job_not_found")
    lease_until = row["lease_until"]
    if (
        row["state"] != expected_state.value
        or int(row["attempts"]) != job.attempt
        or lease_until is None
        or float(lease_until) <= now
    ):
        raise StoreError("job_lease_lost")
    persisted_correlation = row["correlation_id"]
    if require_fence and (not job.correlation_id or not persisted_correlation):
        raise StoreError("job_lease_lost")
    if job.correlation_id and persisted_correlation != job.correlation_id:
        raise StoreError("job_lease_lost")
    expected_correlation = str(persisted_correlation or job.correlation_id or "")
    if outcome_correlation and outcome_correlation != expected_correlation:
        raise StoreError("job_lease_lost")
    return expected_correlation


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow TEXT NOT NULL,
                    title TEXT NOT NULL,
                    harness TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    placement TEXT,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    available_at REAL NOT NULL,
                    lease_until REAL,
                    agent_name TEXT,
                    error_code TEXT,
                    execution_path TEXT,
                    herdr_workspace_id TEXT,
                    receipt_kind TEXT,
                    receipt_value TEXT,
                    agent_settled INTEGER,
                    task_verified INTEGER,
                    error_summary TEXT,
                    correlation_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(workflow, dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_runnable
                    ON jobs(workflow, state, available_at, lease_until);
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id),
                    attempt INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    agent_state TEXT NOT NULL,
                    member_reused INTEGER NOT NULL,
                    pane_id TEXT,
                    error_code TEXT,
                    placement TEXT,
                    execution_path TEXT,
                    herdr_workspace_id TEXT,
                    agent_settled INTEGER,
                    task_verified INTEGER,
                    error_summary TEXT,
                    correlation_id TEXT,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                version = int(row["version"])
                if version == 1:
                    self._migrate_v1_to_v2(connection)
                    version = 2
                if version == 2:
                    self._migrate_v2_to_v3(connection)
                    version = 3
                if version == 3:
                    self._migrate_v3_to_v4(connection)
                    version = 4
                if version != SCHEMA_VERSION:
                    raise StoreError(f"unsupported_schema_version: {version}")

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        self._add_column_if_missing(connection, "jobs", "placement", "TEXT")
        connection.execute(
            "UPDATE jobs SET placement = ? WHERE placement IS NULL",
            (PlacementTarget.TAB.value,),
        )
        self._add_column_if_missing(connection, "jobs", "execution_path", "TEXT")
        self._add_column_if_missing(connection, "jobs", "herdr_workspace_id", "TEXT")
        self._add_column_if_missing(connection, "receipts", "placement", "TEXT")
        connection.execute(
            "UPDATE receipts SET placement = ? WHERE placement IS NULL",
            (PlacementTarget.TAB.value,),
        )
        self._add_column_if_missing(connection, "receipts", "execution_path", "TEXT")
        self._add_column_if_missing(connection, "receipts", "herdr_workspace_id", "TEXT")
        connection.execute("UPDATE schema_meta SET version = 2")

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        self._add_column_if_missing(connection, "jobs", "receipt_kind", "TEXT")
        self._add_column_if_missing(connection, "jobs", "receipt_value", "TEXT")
        self._add_column_if_missing(connection, "jobs", "agent_settled", "INTEGER")
        self._add_column_if_missing(connection, "jobs", "task_verified", "INTEGER")
        self._add_column_if_missing(connection, "jobs", "error_summary", "TEXT")
        self._add_column_if_missing(connection, "receipts", "agent_settled", "INTEGER")
        self._add_column_if_missing(connection, "receipts", "task_verified", "INTEGER")
        self._add_column_if_missing(connection, "receipts", "error_summary", "TEXT")
        connection.execute("UPDATE schema_meta SET version = 3")

    def _migrate_v3_to_v4(self, connection: sqlite3.Connection) -> None:
        self._add_column_if_missing(connection, "jobs", "correlation_id", "TEXT")
        self._add_column_if_missing(connection, "receipts", "correlation_id", "TEXT")
        connection.execute("UPDATE schema_meta SET version = 4")

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def enqueue(self, job: NewJob) -> tuple[int, bool]:
        now = time.time()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs(
                    workflow, title, harness, prompt, dedupe_key, placement, state,
                    attempts, max_attempts, available_at, receipt_kind, receipt_value,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow, dedupe_key) DO NOTHING
                """,
                (
                    job.workflow,
                    job.title,
                    job.harness.value,
                    job.prompt,
                    job.dedupe_key,
                    job.placement.value if job.placement is not None else None,
                    JobState.PENDING.value,
                    job.max_attempts,
                    now,
                    job.receipt.kind.value if job.receipt is not None else None,
                    job.receipt.value if job.receipt is not None else None,
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 1:
                if cursor.lastrowid is None:
                    raise StoreError("job_id_missing")
                return cursor.lastrowid, True
            row = connection.execute(
                """
                SELECT id, title, harness, prompt, placement, receipt_kind, receipt_value
                FROM jobs WHERE workflow = ? AND dedupe_key = ?
                """,
                (job.workflow, job.dedupe_key),
            ).fetchone()
            if row is None:
                raise StoreError("dedupe_lookup_failed")
            if not _job_contract_matches(row, job):
                raise StoreError("dedupe_contract_conflict")
            return int(row["id"]), False

    def existing_job_for_enqueue(
        self,
        workflow: str,
        dedupe_key: str,
        *,
        title: str,
        prompt: str,
        harness: Harness | None,
        placement: PlacementTarget | None,
        receipt: TaskReceipt | None,
    ) -> tuple[int, Harness] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, title, harness, prompt, placement, receipt_kind, receipt_value
                FROM jobs WHERE workflow = ? AND dedupe_key = ?
                """,
                (workflow, dedupe_key),
            ).fetchone()
        if row is None:
            return None
        if not _partial_job_contract_matches(
            row,
            title=title,
            prompt=prompt,
            harness=harness,
            placement=placement,
            receipt=receipt,
        ):
            raise StoreError("dedupe_contract_conflict")
        return int(row["id"]), Harness(str(row["harness"]))

    def existing_job(
        self,
        workflow: str,
        dedupe_key: str,
    ) -> tuple[int, Harness] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, harness FROM jobs WHERE workflow = ? AND dedupe_key = ?",
                (workflow, dedupe_key),
            ).fetchone()
        if row is None:
            return None
        return int(row["id"]), Harness(str(row["harness"]))

    def claim(
        self,
        workflow: str,
        *,
        limit: int,
        lease_seconds: int,
        slot_names: Mapping[str, Sequence[str]] | None = None,
        slot_limits: Mapping[str, int] | None = None,
        allowed_harnesses: Iterable[Harness] | None = None,
    ) -> list[ClaimedJob]:
        if limit <= 0:
            return []
        now = time.time()
        lease_until = now + lease_seconds
        allowed_values = (
            None if allowed_harnesses is None else {harness.value for harness in allowed_harnesses}
        )
        claimed: list[ClaimedJob] = []
        with self._transaction() as connection:
            self._expire_exhausted(connection, workflow, now)
            busy_counts, busy_names = self._busy_slots(connection, workflow, now)
            candidates = self._claim_candidates(connection, workflow, now)
            for row in candidates:
                job = self._claim_candidate(
                    connection,
                    row,
                    now=now,
                    lease_until=lease_until,
                    allowed_values=allowed_values,
                    slot_names=slot_names,
                    slot_limits=slot_limits,
                    busy_counts=busy_counts,
                    busy_names=busy_names,
                )
                if job is None:
                    continue
                claimed.append(job)
                if len(claimed) >= limit:
                    break
        return claimed

    @staticmethod
    def _expire_exhausted(
        connection: sqlite3.Connection,
        workflow: str,
        now: float,
    ) -> None:
        rows = connection.execute(
            """
            SELECT * FROM jobs
            WHERE workflow = ? AND state = ? AND lease_until <= ?
              AND attempts >= max_attempts
            """,
            (workflow, JobState.RUNNING.value, now),
        ).fetchall()
        for row in rows:
            _record_expired_attempt(
                connection,
                row,
                state=JobState.FAILED,
                observed_at=now,
            )
        connection.execute(
            """
            UPDATE jobs
            SET state = ?, error_code = 'lease_expired', lease_until = NULL,
                execution_path = NULL, herdr_workspace_id = NULL,
                agent_settled = NULL, task_verified = NULL, error_summary = NULL,
                updated_at = ?
            WHERE workflow = ? AND state = ? AND lease_until <= ? AND attempts >= max_attempts
            """,
            (JobState.FAILED.value, now, workflow, JobState.RUNNING.value, now),
        )

    @staticmethod
    def _busy_slots(
        connection: sqlite3.Connection,
        workflow: str,
        now: float,
    ) -> tuple[Counter[str], dict[str, set[str]]]:
        rows = connection.execute(
            """
            SELECT harness, placement, agent_name FROM jobs
            WHERE workflow = ? AND state = ? AND lease_until > ?
            """,
            (workflow, JobState.RUNNING.value, now),
        ).fetchall()
        counts: Counter[str] = Counter()
        names: dict[str, set[str]] = {}
        for row in rows:
            harness_value = str(row["harness"])
            counts[harness_value] += 1
            agent_name = row["agent_name"]
            if isinstance(agent_name, str) and agent_name:
                names.setdefault(harness_value, set()).add(agent_name)
        return counts, names

    @staticmethod
    def _claim_candidates(
        connection: sqlite3.Connection,
        workflow: str,
        now: float,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT * FROM jobs
            WHERE workflow = ?
              AND placement IS NOT NULL
              AND attempts < max_attempts
              AND (
                (state = ? AND available_at <= ?)
                OR (state = ? AND lease_until <= ?)
              )
            ORDER BY created_at, id
            """,
            (
                workflow,
                JobState.PENDING.value,
                now,
                JobState.RUNNING.value,
                now,
            ),
        ).fetchall()

    @staticmethod
    def _claim_candidate(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: float,
        lease_until: float,
        allowed_values: set[str] | None,
        slot_names: Mapping[str, Sequence[str]] | None,
        slot_limits: Mapping[str, int] | None,
        busy_counts: Counter[str],
        busy_names: dict[str, set[str]],
    ) -> ClaimedJob | None:
        harness_value = str(row["harness"])
        placement_value = str(row["placement"])
        if allowed_values is not None and harness_value not in allowed_values:
            return None
        names = _candidate_slot_names(row, harness_value, placement_value, slot_names)
        limit = (
            slot_limits.get(harness_value, len(names)) if slot_limits is not None else len(names)
        )
        if busy_counts[harness_value] >= limit:
            return None
        agent_name = next(
            (name for name in names if name not in busy_names.get(harness_value, set())),
            None,
        )
        if agent_name is None:
            return None
        if row["state"] == JobState.RUNNING.value:
            _record_expired_attempt(
                connection,
                row,
                state=JobState.PENDING,
                observed_at=now,
            )
        attempt = int(row["attempts"]) + 1
        correlation_id = uuid.uuid4().hex
        connection.execute(
            """
            UPDATE jobs
            SET state = ?, attempts = ?, lease_until = ?, agent_name = ?,
                error_code = NULL, execution_path = NULL, herdr_workspace_id = NULL,
                agent_settled = NULL, task_verified = NULL, error_summary = NULL,
                correlation_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                JobState.RUNNING.value,
                attempt,
                lease_until,
                agent_name,
                correlation_id,
                now,
                row["id"],
            ),
        )
        busy_counts[harness_value] += 1
        busy_names.setdefault(harness_value, set()).add(agent_name)
        return ClaimedJob(
            job_id=int(row["id"]),
            workflow=str(row["workflow"]),
            title=str(row["title"]),
            harness=Harness(harness_value),
            prompt=str(row["prompt"]),
            dedupe_key=str(row["dedupe_key"]),
            attempt=attempt,
            max_attempts=int(row["max_attempts"]),
            agent_name=agent_name,
            placement=PlacementTarget(placement_value),
            receipt=_receipt_from_row(row),
            correlation_id=correlation_id,
        )

    def record_outcome(self, job: ClaimedJob, outcome: DispatchOutcome) -> JobState:
        normalized = _normalize_outcome(job, outcome, resume=False)
        return self._persist_outcome(
            job,
            outcome,
            normalized,
            expected_state=JobState.RUNNING,
            require_fence=False,
        )

    def _persist_outcome(
        self,
        job: ClaimedJob,
        outcome: DispatchOutcome,
        normalized: _NormalizedOutcome,
        *,
        expected_state: JobState,
        require_fence: bool,
    ) -> JobState:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state, attempts, lease_until, correlation_id FROM jobs WHERE id = ?",
                (job.job_id,),
            ).fetchone()
            expected_correlation = _validate_owned_attempt(
                row,
                job,
                normalized.correlation_id,
                expected_state,
                time.time(),
                require_fence=require_fence,
            )
            correlation_id = expected_correlation or normalized.correlation_id
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, available_at = ?, lease_until = NULL, agent_name = ?,
                    error_code = ?, execution_path = ?, herdr_workspace_id = ?,
                    agent_settled = ?, task_verified = ?, error_summary = ?,
                    correlation_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized.state.value,
                    normalized.available_at,
                    outcome.agent_name,
                    normalized.error_code,
                    outcome.execution_path,
                    outcome.herdr_workspace_id,
                    int(normalized.agent_settled),
                    (
                        int(normalized.task_verified)
                        if normalized.task_verified is not None
                        else None
                    ),
                    normalized.error_summary,
                    correlation_id,
                    normalized.observed_at,
                    job.job_id,
                ),
            )
            _insert_attempt_receipt(
                connection,
                job_id=job.job_id,
                attempt=job.attempt,
                state=normalized.state,
                agent_name=outcome.agent_name,
                agent_state=outcome.state,
                member_reused=outcome.member_reused,
                pane_id=outcome.pane_id,
                error_code=normalized.error_code,
                placement=(
                    outcome.placement.value
                    if outcome.placement is not None
                    else job.placement.value
                ),
                execution_path=outcome.execution_path,
                herdr_workspace_id=outcome.herdr_workspace_id,
                agent_settled=normalized.agent_settled,
                task_verified=normalized.task_verified,
                error_summary=normalized.error_summary,
                correlation_id=correlation_id or None,
                observed_at=normalized.observed_at,
            )
        return normalized.state

    def status_counts(
        self,
        workflow: str,
        *,
        allowed_harnesses: Iterable[Harness] | None = None,
    ) -> dict[str, int]:
        counts = {state.value: 0 for state in JobState}
        allowed_values = (
            None if allowed_harnesses is None else {harness.value for harness in allowed_harnesses}
        )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, harness FROM jobs WHERE workflow = ?",
                (workflow,),
            ).fetchall()
        for row in rows:
            if allowed_values is None or str(row["harness"]) in allowed_values:
                counts[str(row["state"])] += 1
        return counts

    def claim_blocked_for_resume(
        self,
        workflow: str,
        job_id: int,
        *,
        lease_seconds: int,
    ) -> tuple[ClaimedJob, str]:
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT jobs.*, receipts.pane_id AS blocked_pane_id
                FROM jobs
                JOIN receipts ON receipts.id = (
                    SELECT id FROM receipts
                    WHERE receipts.job_id = jobs.id
                    ORDER BY id DESC LIMIT 1
                )
                WHERE jobs.workflow = ? AND jobs.id = ?
                """,
                (workflow, job_id),
            ).fetchone()
            if row is None:
                raise StoreError("job_not_found")
            if row["state"] != JobState.BLOCKED.value:
                raise StoreError("job_not_resumable")
            if row["lease_until"] is not None and float(row["lease_until"]) > now:
                raise StoreError("job_resume_in_progress")
            agent_name = row["agent_name"]
            pane_id = row["blocked_pane_id"]
            if not isinstance(agent_name, str) or not agent_name:
                raise StoreError("blocked_agent_missing")
            if not isinstance(pane_id, str) or not pane_id:
                raise StoreError("blocked_pane_missing")
            placement = row["placement"]
            if not isinstance(placement, str) or not placement:
                raise StoreError("blocked_placement_missing")
            correlation_id = uuid.uuid4().hex
            connection.execute(
                """
                UPDATE jobs
                SET lease_until = ?, correlation_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    now + lease_seconds,
                    correlation_id,
                    now,
                    job_id,
                ),
            )
            job = ClaimedJob(
                job_id=int(row["id"]),
                workflow=str(row["workflow"]),
                title=str(row["title"]),
                harness=Harness(str(row["harness"])),
                prompt=str(row["prompt"]),
                dedupe_key=str(row["dedupe_key"]),
                attempt=int(row["attempts"]),
                max_attempts=int(row["max_attempts"]),
                agent_name=agent_name,
                placement=PlacementTarget(placement),
                receipt=_receipt_from_row(row),
                correlation_id=correlation_id,
            )
        return job, pane_id

    def record_resume_outcome(
        self,
        job: ClaimedJob,
        outcome: DispatchOutcome,
    ) -> JobState:
        normalized = _normalize_outcome(job, outcome, resume=True)
        return self._persist_outcome(
            job,
            outcome,
            normalized,
            expected_state=JobState.BLOCKED,
            require_fence=True,
        )

    def retry_failed(
        self,
        workflow: str,
        job_id: int,
        *,
        extra_attempts: int = 1,
    ) -> dict[str, object]:
        if not 1 <= extra_attempts <= 10:
            raise StoreError("extra_attempts_out_of_range")
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT id, state, attempts, max_attempts
                FROM jobs WHERE workflow = ? AND id = ?
                """,
                (workflow, job_id),
            ).fetchone()
            if row is None:
                raise StoreError("job_not_found")
            if row["state"] != JobState.FAILED.value:
                raise StoreError("job_not_retryable")
            max_attempts = int(row["max_attempts"]) + extra_attempts
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, max_attempts = ?, available_at = ?, lease_until = NULL,
                    error_code = NULL, error_summary = NULL, agent_settled = NULL,
                    task_verified = NULL, correlation_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    JobState.PENDING.value,
                    max_attempts,
                    now,
                    now,
                    job_id,
                ),
            )
        return {
            "job_id": job_id,
            "state": JobState.PENDING.value,
            "attempts": int(row["attempts"]),
            "max_attempts": max_attempts,
        }

    def jobs(self, workflow: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, harness, placement, state, attempts, max_attempts,
                       agent_name, error_code, execution_path, herdr_workspace_id,
                       receipt_kind, receipt_value, agent_settled, task_verified
                       , error_summary, correlation_id
                FROM jobs WHERE workflow = ? ORDER BY id
                """,
                (workflow,),
            ).fetchall()
        jobs = [dict(row) for row in rows]
        for job in jobs:
            job["agent_settled"] = _nullable_bool(job["agent_settled"])
            job["task_verified"] = _nullable_bool(job["task_verified"])
        return jobs

    def created_agent_panes(self, workflow: str) -> dict[str, str]:
        """Return the latest pane recorded when this workflow created an agent."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT receipts.agent_name, receipts.pane_id
                FROM receipts
                JOIN jobs ON jobs.id = receipts.job_id
                WHERE jobs.workflow = ?
                  AND receipts.member_reused = 0
                  AND receipts.pane_id IS NOT NULL
                  AND receipts.placement IN (?, ?)
                ORDER BY receipts.observed_at, receipts.id
                """,
                (
                    workflow,
                    PlacementTarget.TAB.value,
                    PlacementTarget.PANE.value,
                ),
            ).fetchall()
        return {str(row["agent_name"]): str(row["pane_id"]) for row in rows}

    def unplaced_jobs(
        self,
        workflow: str,
        *,
        allowed_harnesses: Iterable[Harness] | None = None,
    ) -> list[dict[str, object]]:
        allowed_values = (
            None if allowed_harnesses is None else {harness.value for harness in allowed_harnesses}
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, harness, prompt, dedupe_key
                FROM jobs
                WHERE workflow = ? AND state = ? AND placement IS NULL
                ORDER BY created_at, id
                """,
                (workflow, JobState.PENDING.value),
            ).fetchall()
        return [
            dict(row)
            for row in rows
            if allowed_values is None or str(row["harness"]) in allowed_values
        ]

    def set_placement(
        self,
        job_id: int,
        placement: PlacementTarget,
    ) -> None:
        now = time.time()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET placement = ?, updated_at = ?
                WHERE id = ? AND state = ? AND attempts = 0 AND placement IS NULL
                """,
                (
                    placement.value,
                    now,
                    job_id,
                    JobState.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("job_placement_not_assignable")

    def metadata_float(self, key: str) -> float | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            return float(row["value"])
        except ValueError as exc:
            raise StoreError(f"metadata_invalid_float: {key}") from exc

    def set_metadata_float(self, key: str, value: float) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, str(value), now),
            )

    def reserve_planner_run(
        self,
        workflow: str,
        interval_seconds: int,
        *,
        now: float | None = None,
    ) -> bool:
        observed_at = time.time() if now is None else now
        key = f"planner_last_attempt:{workflow}"
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                try:
                    last_attempt = float(row["value"])
                except (TypeError, ValueError) as exc:
                    raise StoreError(f"metadata_invalid_float: {key}") from exc
                if observed_at - last_attempt < interval_seconds:
                    return False
            connection.execute(
                """
                INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, str(observed_at), observed_at),
            )
        return True

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
