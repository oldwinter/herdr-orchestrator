"""Harness-health write fencing helpers kept separate from the queue store."""

from __future__ import annotations

import sqlite3

from herdr_orchestrator.attempts import StoreError
from herdr_orchestrator.model import Harness

HEALTH_TABLE_COLUMNS = """
workflow TEXT NOT NULL,
workspace TEXT NOT NULL,
harness TEXT NOT NULL,
status TEXT NOT NULL,
reason TEXT NOT NULL,
source TEXT NOT NULL,
observed_at REAL NOT NULL,
revision INTEGER NOT NULL DEFAULT 0,
expires_at REAL,
cooldown_until REAL,
retryable_failures INTEGER NOT NULL DEFAULT 0,
probe_lease_until REAL,
probe_owner TEXT,
PRIMARY KEY(workflow, workspace, harness)
"""


def migrate_legacy_health_table(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(harness_health)")}
    if "reason_code" not in columns:
        return
    connection.execute(f"""
        CREATE TABLE harness_health_v9 (
            {HEALTH_TABLE_COLUMNS}
        )
        """)
    connection.execute("""
        INSERT INTO harness_health_v9
        SELECT workflow, workspace, harness, status,
               COALESCE(reason_code, CASE WHEN status = 'ready'
                   THEN 'readiness_ready' ELSE 'health_unknown' END),
               source, observed_at, revision, expires_at, cooldown_until,
               consecutive_failures, probe_lease_until, probe_lease_token
        FROM harness_health
        """)
    connection.execute("DROP TABLE harness_health")
    connection.execute("ALTER TABLE harness_health_v9 RENAME TO harness_health")
    connection.execute("""
        CREATE INDEX harness_health_scope ON harness_health(workflow, workspace, harness)
        """)


def normalize_health_write_fence(
    *,
    expected_revision: int | None,
    expected_owner: str | None,
    probe_owner: str | None,
    revision: int | None,
    owner: str | None,
) -> tuple[int | None, str | None, str | None]:
    if revision is not None:
        if expected_revision is not None and expected_revision != revision:
            raise StoreError("health_revision_conflict")
        expected_revision = revision
    if owner is not None:
        if expected_owner is not None and expected_owner != owner:
            raise StoreError("health_owner_conflict")
        expected_owner = owner
    if expected_revision is not None and expected_owner is None and probe_owner is not None:
        expected_owner = probe_owner
        probe_owner = None
    if expected_revision is not None:
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise StoreError("health_revision_invalid")
        if not expected_owner:
            raise StoreError("health_owner_required")
    return expected_revision, expected_owner, probe_owner


def health_row_accepts_write(
    existing: sqlite3.Row,
    *,
    observed_at: float,
    expected_revision: int | None,
    expected_owner: str | None,
) -> bool:
    current_revision = int(existing["revision"])
    persisted_at = float(existing["observed_at"])
    if expected_revision is None:
        if observed_at < persisted_at:
            return False
        return not (observed_at == persisted_at and current_revision != 0)
    return not (
        observed_at < persisted_at
        or current_revision != expected_revision
        or existing["probe_owner"] != expected_owner
    )


def health_update_statement(
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
    probe_lease_until: float | None,
    probe_owner: str | None,
    current_revision: int,
    expected_owner: str | None,
    clear_probe_lease: bool,
) -> tuple[str, list[object]]:
    lease_until_sql = (
        "NULL"
        if clear_probe_lease
        else "harness_health.probe_lease_until" if probe_lease_until is None else "?"
    )
    owner_sql = (
        "NULL"
        if clear_probe_lease
        else "harness_health.probe_owner" if probe_owner is None else "?"
    )
    values: list[object] = [
        status,
        reason,
        source,
        observed_at,
        expires_at,
        cooldown_until,
        retryable_failures,
    ]
    if probe_lease_until is not None and not clear_probe_lease:
        values.append(probe_lease_until)
    if probe_owner is not None and not clear_probe_lease:
        values.append(probe_owner)
    values.extend([workflow, workspace, harness.value, current_revision])
    owner_predicate = ""
    if expected_owner is not None:
        owner_predicate = " AND probe_owner = ?"
        values.append(expected_owner)
    query = f"""
        UPDATE harness_health
        SET status = ?, reason = ?, source = ?, observed_at = ?,
            revision = revision + 1, expires_at = ?, cooldown_until = ?,
            retryable_failures = ?,
            probe_lease_until = {lease_until_sql},
            probe_owner = {owner_sql}
        WHERE workflow = ? AND workspace = ? AND harness = ?
          AND revision = ?{owner_predicate}
    """  # nosec B608: fragments are from a fixed SQL-token allowlist
    return query, values
