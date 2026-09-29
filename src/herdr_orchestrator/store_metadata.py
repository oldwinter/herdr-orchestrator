"""Metadata persistence helpers kept separate from the queue store."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from herdr_orchestrator.attempts import StoreError


def _finite_float(value: object, key: str) -> float:
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
