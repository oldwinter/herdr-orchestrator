"""Metadata persistence helpers kept separate from the queue store."""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from herdr_orchestrator.attempts import StoreError


def _finite_float(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise StoreError(f"metadata_invalid_float: {key}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"metadata_invalid_float: {key}") from exc
    if not math.isfinite(parsed):
        raise StoreError(f"metadata_invalid_float: {key}")
    return parsed


def metadata_float(store: Any, key: str) -> float | None:
    with store._connect() as connection:
        row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return _finite_float(row["value"], key)


def set_metadata_float(store: Any, key: str, value: float) -> None:
    stored_value = _finite_float(value, key)
    now = time.time()
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE
            SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(stored_value), now),
        )


def reserve_planner_run(
    store: Any,
    workflow: str,
    interval_seconds: int,
    *,
    now: float | None = None,
    workspace: str | Path | None = None,
) -> bool:
    key = (
        f"planner_last_attempt:{workflow}:{str(workspace)}"
        if workspace is not None
        else f"planner_last_attempt:{workflow}"
    )
    observed_at = _finite_float(time.time() if now is None else now, key)
    with store._transaction() as connection:
        row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        if row is not None:
            last_attempt = _finite_float(row["value"], key)
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


def migrate_v9_to_v10(connection: sqlite3.Connection) -> None:
    """Index receipts by job and pin schema_meta to a single row."""
    connection.execute("CREATE INDEX IF NOT EXISTS receipts_job_id ON receipts(job_id)")
    connection.execute("""
        CREATE TABLE schema_meta_v10 (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        )
        """)
    connection.execute("""
        INSERT INTO schema_meta_v10(id, version)
        SELECT 1, version FROM schema_meta ORDER BY rowid LIMIT 1
        """)
    connection.execute("DROP TABLE schema_meta")
    connection.execute("ALTER TABLE schema_meta_v10 RENAME TO schema_meta")
    connection.execute("UPDATE schema_meta SET version = 10")
