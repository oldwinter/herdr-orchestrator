from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from herdr_orchestrator.attempts import AttemptLedger, StoreError
from herdr_orchestrator.completion import CompletionPolicy, VerificationClass
from herdr_orchestrator.model import (
    AttemptPhase,
    AttemptProgress,
    ClaimedJob,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
    PlacementTarget,
    TaskReceipt,
)

SCHEMA_VERSION = 7
__all__ = ["SCHEMA_VERSION", "Store", "StoreError"]

_INITIALIZE_LOCK = threading.Lock()


def _nullable_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


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


def _job_contract_matches(row: sqlite3.Row, job: NewJob) -> bool:
    completion_policy = _completion_policy(job.receipt, job.completion_policy)
    return all(
        (
            row["title"] == job.title,
            row["harness"] == job.harness.value,
            row["prompt"] == job.prompt,
            row["placement"] == (job.placement.value if job.placement is not None else None),
            row["receipt_kind"] == (job.receipt.kind.value if job.receipt is not None else None),
            row["receipt_value"] == (job.receipt.value if job.receipt is not None else None),
            row["completion_policy"] == completion_policy.value,
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
    completion_policy: CompletionPolicy | None,
) -> bool:
    effective_policy = _completion_policy(receipt, completion_policy)
    return all(
        (
            row["title"] == title,
            row["prompt"] == prompt,
            harness is None or row["harness"] == harness.value,
            placement is None or row["placement"] == placement.value,
            row["receipt_kind"] == (receipt.kind.value if receipt is not None else None),
            row["receipt_value"] == (receipt.value if receipt is not None else None),
            row["completion_policy"] == effective_policy.value,
        )
    )


def _completion_policy(
    receipt: TaskReceipt | None,
    requested: CompletionPolicy | None,
) -> CompletionPolicy:
    if requested is None:
        return (
            CompletionPolicy.RECEIPT_V1
            if receipt is not None
            else CompletionPolicy.LEGACY_UNVERIFIED
        )
    if requested is CompletionPolicy.STRUCTURED_V2 and receipt is None:
        return requested
    if requested is CompletionPolicy.RECEIPT_V1 and receipt is not None:
        return requested
    if requested is CompletionPolicy.LEGACY_UNVERIFIED and receipt is None:
        return requested
    raise StoreError("completion_policy_invalid")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        with _INITIALIZE_LOCK:
            self._initialize()

    def _initialize(self) -> None:
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
                    completion_policy TEXT NOT NULL DEFAULT 'legacy-unverified',
                    verification_class TEXT NOT NULL DEFAULT 'unverified',
                    completion_status TEXT,
                    completion_evidence_summary TEXT,
                    completion_error_code TEXT,
                    agent_settled INTEGER,
                    task_verified INTEGER,
                    error_summary TEXT,
                    correlation_id TEXT,
                    current_attempt_id INTEGER,
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
                    completion_policy TEXT NOT NULL DEFAULT 'legacy-unverified',
                    verification_class TEXT NOT NULL DEFAULT 'unverified',
                    completion_status TEXT,
                    completion_evidence_summary TEXT,
                    completion_error_code TEXT,
                    error_summary TEXT,
                    correlation_id TEXT,
                    attempt_id INTEGER,
                    fencing_token TEXT,
                    operation_token TEXT,
                    operation_sequence INTEGER,
                    event_kind TEXT,
                    is_stale INTEGER NOT NULL DEFAULT 0,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS harness_health (
                    workflow TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    harness TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    expires_at REAL,
                    cooldown_until REAL,
                    retryable_failures INTEGER NOT NULL DEFAULT 0,
                    probe_lease_until REAL,
                    probe_owner TEXT,
                    PRIMARY KEY(workflow, workspace, harness)
                );
                CREATE INDEX IF NOT EXISTS harness_health_scope
                    ON harness_health(workflow, workspace, harness);
                """)
            AttemptLedger.create_schema(connection)
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
                if version == 4:
                    self._migrate_v4_to_v5(connection)
                    version = 5
                if version == 5:
                    self._migrate_v5_to_v6(connection)
                    version = 6
                if version == 6:
                    self._migrate_v6_to_v7(connection)
                    version = 7
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

    def _migrate_v4_to_v5(self, connection: sqlite3.Connection) -> None:
        AttemptLedger.migrate_v4_to_v5(connection, self._add_column_if_missing)

    def _migrate_v5_to_v6(self, connection: sqlite3.Connection) -> None:
        for table in ("jobs", "job_attempts", "receipts"):
            self._add_column_if_missing(connection, table, "completion_policy", "TEXT")
            self._add_column_if_missing(connection, table, "verification_class", "TEXT")
            self._add_column_if_missing(connection, table, "completion_status", "TEXT")
            self._add_column_if_missing(
                connection,
                table,
                "completion_evidence_summary",
                "TEXT",
            )
            self._add_column_if_missing(connection, table, "completion_error_code", "TEXT")
        connection.execute(
            """
            UPDATE jobs
            SET completion_policy = CASE
                    WHEN receipt_kind IS NULL THEN ? ELSE ? END,
                verification_class = CASE
                    WHEN receipt_kind IS NULL THEN ?
                    WHEN task_verified = 1 THEN ?
                    WHEN task_verified = 0 THEN ?
                    ELSE ? END,
                completion_status = CASE
                    WHEN receipt_kind IS NOT NULL AND task_verified = 1 THEN ?
                    ELSE NULL END,
                completion_error_code = CASE
                    WHEN receipt_kind IS NOT NULL AND task_verified = 0
                    THEN COALESCE(error_code, 'completion_verification_failed')
                    ELSE NULL END
            """,
            (
                CompletionPolicy.LEGACY_UNVERIFIED.value,
                CompletionPolicy.RECEIPT_V1.value,
                VerificationClass.UNVERIFIED.value,
                VerificationClass.VERIFIED.value,
                VerificationClass.VERIFICATION_FAILED.value,
                VerificationClass.UNVERIFIED.value,
                "completed",
            ),
        )
        evidence_values = (
            CompletionPolicy.LEGACY_UNVERIFIED.value,
            VerificationClass.UNVERIFIED.value,
            VerificationClass.VERIFIED.value,
            VerificationClass.VERIFICATION_FAILED.value,
            VerificationClass.UNVERIFIED.value,
            "completed",
        )
        connection.execute(
            """
            UPDATE job_attempts
            SET completion_policy = (
                    SELECT jobs.completion_policy
                    FROM jobs WHERE jobs.id = job_attempts.job_id
                ),
                verification_class = CASE
                    WHEN (
                        SELECT jobs.completion_policy
                        FROM jobs WHERE jobs.id = job_attempts.job_id
                    ) = ? THEN ?
                    WHEN task_verified = 1 THEN ?
                    WHEN task_verified = 0 THEN ?
                    ELSE ? END,
                completion_status = CASE
                    WHEN task_verified = 1 THEN ? ELSE NULL END,
                completion_error_code = CASE
                    WHEN task_verified = 0
                    THEN COALESCE(error_code, 'completion_verification_failed')
                    ELSE NULL END
            """,
            evidence_values,
        )
        connection.execute(
            """
            UPDATE receipts
            SET completion_policy = (
                    SELECT jobs.completion_policy FROM jobs WHERE jobs.id = receipts.job_id
                ),
                verification_class = CASE
                    WHEN (
                        SELECT jobs.completion_policy FROM jobs WHERE jobs.id = receipts.job_id
                    ) = ? THEN ?
                    WHEN task_verified = 1 THEN ?
                    WHEN task_verified = 0 THEN ?
                    ELSE ? END,
                completion_status = CASE
                    WHEN task_verified = 1 THEN ? ELSE NULL END,
                completion_error_code = CASE
                    WHEN task_verified = 0
                    THEN COALESCE(error_code, 'completion_verification_failed')
                    ELSE NULL END
            """,
            evidence_values,
        )
        connection.execute("UPDATE schema_meta SET version = 6")

    def _migrate_v6_to_v7(self, connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS harness_health (
                workflow TEXT NOT NULL,
                workspace TEXT NOT NULL,
                harness TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at REAL NOT NULL,
                expires_at REAL,
                cooldown_until REAL,
                retryable_failures INTEGER NOT NULL DEFAULT 0,
                probe_lease_until REAL,
                probe_owner TEXT,
                PRIMARY KEY(workflow, workspace, harness)
            )
            """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS harness_health_scope
                ON harness_health(workflow, workspace, harness)
            """)
        connection.execute("UPDATE schema_meta SET version = 7")

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
        completion_policy = _completion_policy(job.receipt, job.completion_policy)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs(
                    workflow, title, harness, prompt, dedupe_key, placement, state,
                    attempts, max_attempts, available_at, receipt_kind, receipt_value,
                    completion_policy, verification_class, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    completion_policy.value,
                    VerificationClass.UNVERIFIED.value,
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
                SELECT id, title, harness, prompt, placement, receipt_kind, receipt_value,
                       completion_policy
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
        completion_policy: CompletionPolicy | None = None,
    ) -> tuple[int, Harness] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, title, harness, prompt, placement, receipt_kind, receipt_value,
                       completion_policy
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
            completion_policy=completion_policy,
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
              AND (
                (state = ? AND available_at <= ? AND attempts < max_attempts)
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
        if row["state"] == JobState.RUNNING.value:
            persisted_name = row["agent_name"]
            if not isinstance(persisted_name, str) or not persisted_name:
                raise StoreError("attempt_agent_missing")
            if persisted_name in busy_names.get(harness_value, set()):
                return None
            recovered = AttemptLedger.reclaim(
                connection,
                row,
                now=now,
                lease_until=lease_until,
            )
            busy_counts[harness_value] += 1
            busy_names.setdefault(harness_value, set()).add(persisted_name)
            return recovered
        agent_name = next(
            (name for name in names if name not in busy_names.get(harness_value, set())),
            None,
        )
        if agent_name is None:
            return None
        claimed = AttemptLedger.create_claim(
            connection,
            row,
            agent_name=agent_name,
            now=now,
            lease_until=lease_until,
        )
        busy_counts[harness_value] += 1
        busy_names.setdefault(harness_value, set()).add(agent_name)
        return claimed

    def record_attempt_progress(
        self,
        job: ClaimedJob,
        progress: AttemptProgress,
    ) -> None:
        with self._transaction() as connection:
            recorded = AttemptLedger.record_progress(
                connection,
                job,
                progress,
                now=time.time(),
            )
        if not recorded:
            raise StoreError("job_lease_lost")

    def record_outcome(self, job: ClaimedJob, outcome: DispatchOutcome) -> JobState:
        with self._transaction() as connection:
            state, recorded = AttemptLedger.record_outcome(
                connection,
                job,
                outcome,
                resume=False,
                now=time.time(),
            )
        if not recorded:
            raise StoreError("job_lease_lost")
        return state

    def attempt_phase(self, attempt_id: int) -> AttemptPhase:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT phase FROM job_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise StoreError("current_attempt_missing")
        return AttemptPhase(str(row["phase"]))

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

    def harness_health_rows(
        self,
        workflow: str,
        workspace: str,
        *,
        harnesses: Iterable[Harness] | None = None,
    ) -> list[dict[str, object]]:
        """Read privacy-safe health rows for one workflow/workspace scope."""
        values = None if harnesses is None else tuple(harness.value for harness in harnesses)
        query = """
            SELECT workflow, workspace, harness, status, reason, source,
                   observed_at, expires_at, cooldown_until, retryable_failures,
                   probe_lease_until, probe_owner
            FROM harness_health
            WHERE workflow = ? AND workspace = ?
        """
        parameters: tuple[object, ...] = (workflow, workspace)
        if values is not None and not values:
            return []
        if values is not None:
            placeholders = ", ".join("?" for _ in values)
            query += f" AND harness IN ({placeholders})"
            parameters += values
        query += " ORDER BY harness"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def upsert_harness_health(
        self,
        *,
        workflow: str,
        workspace: str,
        harness: Harness,
        status: str,
        reason: str,
        source: str,
        observed_at: float,
        expires_at: float | None,
        cooldown_until: float | None,
        retryable_failures: int,
        probe_lease_until: float | None = None,
        probe_owner: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO harness_health(
                    workflow, workspace, harness, status, reason, source,
                    observed_at, expires_at, cooldown_until, retryable_failures,
                    probe_lease_until, probe_owner
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow, workspace, harness) DO UPDATE SET
                    status = excluded.status,
                    reason = excluded.reason,
                    source = excluded.source,
                    observed_at = excluded.observed_at,
                    expires_at = excluded.expires_at,
                    cooldown_until = excluded.cooldown_until,
                    retryable_failures = excluded.retryable_failures,
                    probe_lease_until = CASE
                        WHEN excluded.probe_lease_until IS NULL
                        THEN harness_health.probe_lease_until
                        ELSE excluded.probe_lease_until END,
                    probe_owner = CASE
                        WHEN excluded.probe_owner IS NULL
                        THEN harness_health.probe_owner
                        ELSE excluded.probe_owner END
                """,
                (
                    workflow,
                    workspace,
                    harness.value,
                    status,
                    reason,
                    source,
                    observed_at,
                    expires_at,
                    cooldown_until,
                    retryable_failures,
                    probe_lease_until,
                    probe_owner,
                ),
            )

    def ensure_harness_health(
        self,
        *,
        workflow: str,
        workspace: str,
        harness: Harness,
        observed_at: float,
    ) -> None:
        """Create the unknown baseline without overwriting a concurrent probe lease."""
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO harness_health(
                    workflow, workspace, harness, status, reason, source,
                    observed_at, retryable_failures
                ) VALUES (?, ?, ?, 'unknown', 'health_unknown', 'none', ?, 0)
                ON CONFLICT(workflow, workspace, harness) DO NOTHING
                """,
                (workflow, workspace, harness.value, observed_at),
            )

    def acquire_harness_probe(
        self,
        *,
        workflow: str,
        workspace: str,
        harness: Harness,
        owner: str,
        now: float,
        lease_seconds: float,
    ) -> bool:
        """Atomically reserve one readiness refresh without touching task leases."""
        lease_until = now + lease_seconds
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO harness_health(
                    workflow, workspace, harness, status, reason, source,
                    observed_at, retryable_failures
                ) VALUES (?, ?, ?, 'unknown', 'health_unknown', 'none', ?, 0)
                ON CONFLICT(workflow, workspace, harness) DO NOTHING
                """,
                (workflow, workspace, harness.value, now),
            )
            row = connection.execute(
                """
                SELECT status, expires_at, cooldown_until, probe_lease_until, probe_owner
                FROM harness_health
                WHERE workflow = ? AND workspace = ? AND harness = ?
                """,
                (workflow, workspace, harness.value),
            ).fetchone()
            active_until = row["probe_lease_until"] if row is not None else None
            active_owner = row["probe_owner"] if row is not None else None
            if row is not None:
                status = str(row["status"])
                expires_at = row["expires_at"]
                cooldown_until = row["cooldown_until"]
                if status == "ready" and isinstance(expires_at, (int, float)) and expires_at > now:
                    return False
                if (
                    status in {"degraded", "unavailable"}
                    and isinstance(cooldown_until, (int, float))
                    and cooldown_until > now
                ):
                    return False
            if (
                isinstance(active_until, (int, float))
                and active_until > now
                and active_owner != owner
            ):
                return False
            connection.execute(
                """
                UPDATE harness_health
                SET probe_lease_until = ?, probe_owner = ?
                WHERE workflow = ? AND workspace = ? AND harness = ?
                """,
                (lease_until, owner, workflow, workspace, harness.value),
            )
        return True

    def release_harness_probe(
        self,
        *,
        workflow: str,
        workspace: str,
        harness: Harness,
        owner: str,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE harness_health
                SET probe_lease_until = NULL, probe_owner = NULL
                WHERE workflow = ? AND workspace = ? AND harness = ?
                  AND probe_owner = ?
                """,
                (workflow, workspace, harness.value, owner),
            )

    def pending_harnesses(self, workflow: str) -> tuple[Harness, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT harness FROM jobs
                WHERE workflow = ? AND state = ?
                ORDER BY harness
                """,
                (workflow, JobState.PENDING.value),
            ).fetchall()
        return tuple(Harness(str(row["harness"])) for row in rows)

    def claim_blocked_for_resume(
        self,
        workflow: str,
        job_id: int,
        *,
        lease_seconds: int,
    ) -> tuple[ClaimedJob, str]:
        now = time.time()
        with self._transaction() as connection:
            return AttemptLedger.claim_resume(
                connection,
                workflow,
                job_id,
                lease_seconds=lease_seconds,
                now=now,
            )

    def record_resume_outcome(
        self,
        job: ClaimedJob,
        outcome: DispatchOutcome,
    ) -> JobState:
        with self._transaction() as connection:
            state, recorded = AttemptLedger.record_outcome(
                connection,
                job,
                outcome,
                resume=True,
                now=time.time(),
            )
        if not recorded:
            raise StoreError("job_lease_lost")
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
                    task_verified = NULL, verification_class = ?, completion_status = NULL,
                    completion_evidence_summary = NULL, completion_error_code = NULL,
                    correlation_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    JobState.PENDING.value,
                    max_attempts,
                    now,
                    VerificationClass.UNVERIFIED.value,
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
                SELECT jobs.id, jobs.title, jobs.harness, jobs.placement, jobs.state,
                       jobs.attempts, jobs.max_attempts, jobs.agent_name,
                       jobs.error_code, jobs.execution_path, jobs.herdr_workspace_id,
                       jobs.receipt_kind, jobs.receipt_value, jobs.agent_settled,
                       jobs.task_verified, jobs.error_summary, jobs.correlation_id,
                       jobs.completion_policy, jobs.verification_class,
                       jobs.completion_status, jobs.completion_evidence_summary,
                       jobs.completion_error_code, jobs.current_attempt_id,
                       job_attempts.phase AS attempt_phase
                FROM jobs
                LEFT JOIN job_attempts ON job_attempts.id = jobs.current_attempt_id
                WHERE jobs.workflow = ? ORDER BY jobs.id
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
                  AND receipts.is_stale = 0
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
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
