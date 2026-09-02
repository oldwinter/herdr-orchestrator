"""Workspace-scoped queue read helpers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from herdr_orchestrator.model import Harness, JobState, PlacementTarget


def workspace_matches(
    value: object,
    workspace: str,
    *,
    include_legacy: bool,
    has_scoped_jobs: bool,
) -> bool:
    return value == workspace or include_legacy and value is None and not has_scoped_jobs


def workspace_clause(
    workspace: str | Path | None,
    *,
    include_legacy: bool,
    alias: str = "jobs",
) -> tuple[str, tuple[object, ...]]:
    if workspace is None:
        return "", ()
    target = str(workspace)
    if not include_legacy:
        return f" AND {alias}.workspace = ?", (target,)
    return (
        f""" AND (
            {alias}.workspace = ?
            OR (
                {alias}.workspace IS NULL
                AND NOT EXISTS (
                    SELECT 1 FROM jobs AS scoped_jobs
                    WHERE scoped_jobs.workflow = {alias}.workflow
                      AND scoped_jobs.workspace IS NOT NULL
                )
            )
        )
        """,  # nosec B608: alias is a local SQL identifier, values are bound
        (target,),
    )


def created_agent_panes(
    store: Any,
    workflow: str,
    *,
    workspace: str | Path | None = None,
    include_legacy: bool = False,
) -> dict[str, str]:
    query = """
        SELECT receipts.agent_name, receipts.pane_id
        FROM receipts
        JOIN jobs ON jobs.id = receipts.job_id
        WHERE jobs.workflow = ?
          AND receipts.member_reused = 0
          AND receipts.is_stale = 0
          AND receipts.pane_id IS NOT NULL
          AND receipts.placement IN (?, ?)
    """
    parameters: list[object] = [
        workflow,
        PlacementTarget.TAB.value,
        PlacementTarget.PANE.value,
    ]
    scope, scope_parameters = workspace_clause(
        workspace,
        include_legacy=include_legacy,
    )
    query += scope
    parameters.extend(scope_parameters)
    query += " ORDER BY receipts.observed_at, receipts.id"
    with store._connect() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return {str(row["agent_name"]): str(row["pane_id"]) for row in rows}


def unplaced_jobs(
    store: Any,
    workflow: str,
    *,
    allowed_harnesses: Iterable[Harness] | None = None,
    workspace: str | Path | None = None,
    include_legacy: bool = False,
) -> list[dict[str, object]]:
    allowed_values = (
        None if allowed_harnesses is None else {harness.value for harness in allowed_harnesses}
    )
    query = """
        SELECT id, title, harness, prompt, dedupe_key
        FROM jobs AS jobs
        WHERE jobs.workflow = ? AND jobs.state = ? AND jobs.placement IS NULL
    """
    parameters: tuple[object, ...] = (workflow, JobState.PENDING.value)
    scope, scope_parameters = workspace_clause(
        workspace,
        include_legacy=include_legacy,
    )
    query += scope
    parameters += scope_parameters
    query += " ORDER BY created_at, id"
    with store._connect() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [
        dict(row) for row in rows if allowed_values is None or str(row["harness"]) in allowed_values
    ]
