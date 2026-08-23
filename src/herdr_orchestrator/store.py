from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from herdr_orchestrator.model import (
    AgentState,
    ClaimedJob,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
)

SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    pass


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
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    available_at REAL NOT NULL,
                    lease_until REAL,
                    agent_name TEXT,
                    error_code TEXT,
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
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise StoreError(f"unsupported_schema_version: {row['version']}")

    def enqueue(self, job: NewJob) -> tuple[int, bool]:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs(
                    workflow, title, harness, prompt, dedupe_key, state,
                    attempts, max_attempts, available_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(workflow, dedupe_key) DO NOTHING
                """,
                (
                    job.workflow,
                    job.title,
                    job.harness.value,
                    job.prompt,
                    job.dedupe_key,
                    JobState.PENDING.value,
                    job.max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 1:
                return int(cursor.lastrowid), True
            row = connection.execute(
                "SELECT id FROM jobs WHERE workflow = ? AND dedupe_key = ?",
                (job.workflow, job.dedupe_key),
            ).fetchone()
            if row is None:
                raise StoreError("dedupe_lookup_failed")
            return int(row["id"]), False

    def claim(self, workflow: str, *, limit: int, lease_seconds: int) -> list[ClaimedJob]:
        now = time.time()
        lease_until = now + lease_seconds
        claimed: list[ClaimedJob] = []
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, error_code = 'lease_expired', lease_until = NULL, updated_at = ?
                WHERE workflow = ? AND state = ? AND lease_until <= ? AND attempts >= max_attempts
                """,
                (
                    JobState.FAILED.value,
                    now,
                    workflow,
                    JobState.RUNNING.value,
                    now,
                ),
            )
            active_rows = connection.execute(
                """
                SELECT DISTINCT harness FROM jobs
                WHERE workflow = ? AND state = ? AND lease_until > ?
                """,
                (workflow, JobState.RUNNING.value, now),
            ).fetchall()
            busy_harnesses = {str(row["harness"]) for row in active_rows}
            candidates = connection.execute(
                """
                SELECT * FROM jobs
                WHERE workflow = ?
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
            for row in candidates:
                harness_value = str(row["harness"])
                if harness_value in busy_harnesses:
                    continue
                attempt = int(row["attempts"]) + 1
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, attempts = ?, lease_until = ?, error_code = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        JobState.RUNNING.value,
                        attempt,
                        lease_until,
                        now,
                        row["id"],
                    ),
                )
                claimed.append(
                    ClaimedJob(
                        job_id=int(row["id"]),
                        workflow=str(row["workflow"]),
                        title=str(row["title"]),
                        harness=Harness(str(row["harness"])),
                        prompt=str(row["prompt"]),
                        dedupe_key=str(row["dedupe_key"]),
                        attempt=attempt,
                        max_attempts=int(row["max_attempts"]),
                    )
                )
                busy_harnesses.add(harness_value)
                if len(claimed) >= limit:
                    break
        return claimed

    def record_outcome(self, job: ClaimedJob, outcome: DispatchOutcome) -> JobState:
        now = time.time()
        if outcome.error_code is None and outcome.state in {AgentState.IDLE, AgentState.DONE}:
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

        error_code = outcome.error_code
        if error_code is None and outcome.state in {AgentState.WORKING, AgentState.UNKNOWN}:
            error_code = "agent_not_settled"

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state, attempts FROM jobs WHERE id = ?",
                (job.job_id,),
            ).fetchone()
            if row is None:
                raise StoreError("job_not_found")
            if row["state"] != JobState.RUNNING.value or int(row["attempts"]) != job.attempt:
                raise StoreError("job_lease_lost")
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, available_at = ?, lease_until = NULL, agent_name = ?,
                    error_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    state.value,
                    available_at,
                    outcome.agent_name,
                    error_code,
                    now,
                    job.job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO receipts(
                    job_id, attempt, state, agent_name, agent_state,
                    member_reused, pane_id, error_code, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    now,
                ),
            )
        return state

    def status_counts(self, workflow: str) -> dict[str, int]:
        counts = {state.value: 0 for state in JobState}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS total FROM jobs WHERE workflow = ? GROUP BY state",
                (workflow,),
            ).fetchall()
        for row in rows:
            counts[str(row["state"])] = int(row["total"])
        return counts

    def jobs(self, workflow: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, harness, state, attempts, max_attempts, agent_name, error_code
                FROM jobs WHERE workflow = ? ORDER BY id
                """,
                (workflow,),
            ).fetchall()
        return [dict(row) for row in rows]

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
