"""Metadata persistence helpers kept separate from the queue store."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from herdr_orchestrator.attempts import StoreError


def metadata_float(store: Any, key: str) -> float | None:
    with store._connect() as connection:
        row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return float(row["value"])
    except ValueError as exc:
        raise StoreError(f"metadata_invalid_float: {key}") from exc


def set_metadata_float(store: Any, key: str, value: float) -> None:
    now = time.time()
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE
            SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(value), now),
        )


def reserve_planner_run(
    store: Any,
    workflow: str,
    interval_seconds: int,
    *,
    now: float | None = None,
    workspace: str | Path | None = None,
) -> bool:
    observed_at = time.time() if now is None else now
    key = (
        f"planner_last_attempt:{workflow}:{str(workspace)}"
        if workspace is not None
        else f"planner_last_attempt:{workflow}"
    )
    with store._transaction() as connection:
        row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
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
