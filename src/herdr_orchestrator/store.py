from __future__ import annotations

import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
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


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
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
                """
            )
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
        with self._connect() as connection:
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
                "SELECT id FROM jobs WHERE workflow = ? AND dedupe_key = ?",
                (job.workflow, job.dedupe_key),
            ).fetchone()
            if row is None:
                raise StoreError("dedupe_lookup_failed")
            return int(row["id"]), False

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
        connection.execute(
            """
            UPDATE jobs
            SET state = ?, error_code = 'lease_expired', lease_until = NULL, updated_at = ?
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
        now = time.time()
        error_code = outcome.error_code
        if job.receipt is not None and outcome.task_verified is not True and error_code is None:
            error_code = "task_receipt_missing"
        elif outcome.task_verified is False and error_code is None:
            error_code = "task_receipt_invalid"
        agent_settled = (
            outcome.agent_settled
            if outcome.agent_settled is not None
            else outcome.state in {AgentState.IDLE, AgentState.DONE}
        )
        if (
            error_code is None
            and outcome.state in {AgentState.IDLE, AgentState.DONE}
            and not agent_settled
        ):
            error_code = "agent_not_settled"
        if error_code is None and outcome.state in {AgentState.IDLE, AgentState.DONE}:
            state = JobState.SUCCEEDED
            available_at = now
        elif outcome.state is AgentState.BLOCKED:
            state = JobState.BLOCKED
            available_at = now
        elif job.attempt < job.max_attempts:
            state = JobState.PENDING
            available_at = now + min(60, 2 ** max(0, job.attempt - 1))
        else:
            state = JobState.FAILED
            available_at = now

        if error_code is None and outcome.state in {AgentState.WORKING, AgentState.UNKNOWN}:
            error_code = "agent_not_settled"
        error_summary = _bounded_error_summary(outcome.error_summary)
        correlation_id = outcome.correlation_id or job.correlation_id

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state, attempts, lease_until, correlation_id FROM jobs WHERE id = ?",
                (job.job_id,),
            ).fetchone()
            if row is None:
                raise StoreError("job_not_found")
            if (
                row["state"] != JobState.RUNNING.value
                or int(row["attempts"]) != job.attempt
                or row["lease_until"] is None
                or float(row["lease_until"]) <= now
            ):
                raise StoreError("job_lease_lost")
            persisted_correlation = row["correlation_id"]
            if job.correlation_id and persisted_correlation != job.correlation_id:
                raise StoreError("job_lease_lost")
            expected_correlation = str(persisted_correlation or job.correlation_id or "")
            if correlation_id and correlation_id != expected_correlation:
                raise StoreError("job_lease_lost")
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
                    state.value,
                    available_at,
                    outcome.agent_name,
                    error_code,
                    outcome.execution_path,
                    outcome.herdr_workspace_id,
                    int(agent_settled),
                    (int(outcome.task_verified) if outcome.task_verified is not None else None),
                    error_summary,
                    correlation_id,
                    now,
                    job.job_id,
                ),
            )
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
                    job.job_id,
                    job.attempt,
                    state.value,
                    outcome.agent_name,
                    outcome.state.value,
                    int(outcome.member_reused),
                    outcome.pane_id,
                    error_code,
                    (
                        outcome.placement.value
                        if outcome.placement is not None
                        else job.placement.value
                    ),
                    outcome.execution_path,
                    outcome.herdr_workspace_id,
                    int(agent_settled),
                    (int(outcome.task_verified) if outcome.task_verified is not None else None),
                    error_summary,
                    correlation_id,
                    now,
                ),
            )
        return state

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
        now = time.time()
        error_code = outcome.error_code
        if job.receipt is not None and outcome.task_verified is not True and error_code is None:
            error_code = "task_receipt_missing"
        elif outcome.task_verified is False and error_code is None:
            error_code = "task_receipt_invalid"
        agent_settled = (
            outcome.agent_settled
            if outcome.agent_settled is not None
            else outcome.state in {AgentState.IDLE, AgentState.DONE}
        )
        if (
            error_code is None
            and outcome.state in {AgentState.IDLE, AgentState.DONE}
            and not agent_settled
        ):
            error_code = "agent_not_settled"
        if error_code is None and outcome.state in {AgentState.IDLE, AgentState.DONE}:
            state = JobState.SUCCEEDED
        else:
            state = JobState.BLOCKED
        if error_code is None and outcome.state is AgentState.BLOCKED:
            error_code = "agent_blocked"
        elif error_code is None and outcome.state in {AgentState.WORKING, AgentState.UNKNOWN}:
            error_code = "agent_not_settled"
        error_summary = _bounded_error_summary(outcome.error_summary)
        correlation_id = outcome.correlation_id or job.correlation_id

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state, attempts, lease_until, correlation_id FROM jobs WHERE id = ?",
                (job.job_id,),
            ).fetchone()
            if row is None:
                raise StoreError("job_not_found")
            if (
                row["state"] != JobState.BLOCKED.value
                or int(row["attempts"]) != job.attempt
                or row["lease_until"] is None
                or float(row["lease_until"]) <= now
            ):
                raise StoreError("job_lease_lost")
            persisted_correlation = row["correlation_id"]
            if job.correlation_id and persisted_correlation != job.correlation_id:
                raise StoreError("job_lease_lost")
            expected_correlation = str(persisted_correlation or job.correlation_id or "")
            if correlation_id and correlation_id != expected_correlation:
                raise StoreError("job_lease_lost")
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, available_at = ?, lease_until = NULL, agent_name = ?,
                    error_code = ?, execution_path = ?, herdr_workspace_id = ?,
                    agent_settled = ?, task_verified = ?, error_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    state.value,
                    now,
                    outcome.agent_name,
                    error_code,
                    outcome.execution_path,
                    outcome.herdr_workspace_id,
                    int(agent_settled),
                    (int(outcome.task_verified) if outcome.task_verified is not None else None),
                    error_summary,
                    now,
                    job.job_id,
                ),
            )
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
                    job.job_id,
                    job.attempt,
                    state.value,
                    outcome.agent_name,
                    outcome.state.value,
                    int(outcome.member_reused),
                    outcome.pane_id,
                    error_code,
                    (
                        outcome.placement.value
                        if outcome.placement is not None
                        else job.placement.value
                    ),
                    outcome.execution_path,
                    outcome.herdr_workspace_id,
                    int(agent_settled),
                    (int(outcome.task_verified) if outcome.task_verified is not None else None),
                    error_summary,
                    expected_correlation or None,
                    now,
                ),
            )
        return state

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
