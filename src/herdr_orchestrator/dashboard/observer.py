from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from herdr_orchestrator.protocol import (
    Command,
    CommandRunner,
    TransportError,
    run_json,
    subprocess_runner,
)

CONTROL_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class QueueObservation:
    jobs: tuple[dict[str, object], ...]
    receipts: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class HerdrObservation:
    health: str
    error_code: str | None
    workspaces: tuple[dict[str, object], ...]
    tabs: tuple[dict[str, object], ...]
    panes: tuple[dict[str, object], ...]
    agents: tuple[dict[str, object], ...]
    worktrees: tuple[dict[str, object], ...]

    @classmethod
    def unavailable(cls, error_code: str) -> HerdrObservation:
        return cls("unavailable", error_code, (), (), (), (), ())


class SqliteObserver:
    def __init__(self, path: Path, workflow: str) -> None:
        self.path = path
        self.workflow = workflow

    def observe(self) -> QueueObservation:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        try:
            jobs = connection.execute(
                """
                SELECT id, title, harness, dedupe_key, placement, state, attempts,
                       max_attempts, available_at, lease_until, agent_name, error_code,
                       execution_path, herdr_workspace_id, receipt_kind, agent_settled,
                       task_verified, error_summary, correlation_id, created_at, updated_at
                FROM jobs
                WHERE workflow = ?
                ORDER BY created_at, id
                """,
                (self.workflow,),
            ).fetchall()
            receipts = connection.execute(
                """
                SELECT receipts.id, receipts.job_id, receipts.attempt, receipts.state,
                       receipts.agent_name, receipts.agent_state, receipts.member_reused,
                       receipts.pane_id, receipts.error_code, receipts.placement,
                       receipts.execution_path, receipts.herdr_workspace_id,
                       receipts.agent_settled, receipts.task_verified,
                       receipts.correlation_id, receipts.observed_at
                FROM receipts
                JOIN jobs ON jobs.id = receipts.job_id
                WHERE jobs.workflow = ?
                ORDER BY receipts.observed_at, receipts.id
                """,
                (self.workflow,),
            ).fetchall()
        finally:
            connection.close()
        return QueueObservation(
            tuple(dict(row) for row in jobs),
            tuple(dict(row) for row in receipts),
        )


class HerdrObserver:
    def __init__(
        self,
        workspace: Path,
        *,
        runner: CommandRunner = subprocess_runner,
    ) -> None:
        self.workspace = workspace.resolve()
        self.runner = runner

    def observe(self) -> HerdrObservation:
        try:
            agents, workspaces, worktrees = self._base_rows()
            workspace_ids = self._relevant_workspace_ids(
                workspaces,
                agents,
                worktrees,
            )
            tabs, panes = self._workspace_rows(workspace_ids)
            panes = _scoped_panes(tabs, panes, self.workspace)
            agents = _scoped_agents(agents, self.workspace)
        except TransportError as exc:
            return HerdrObservation.unavailable(exc.code)

        return HerdrObservation(
            health="ok",
            error_code=None,
            workspaces=tuple(
                _safe_fields(row, WORKSPACE_FIELDS)
                for row in workspaces
                if row.get("workspace_id") in workspace_ids
            ),
            tabs=tuple(_safe_fields(row, TAB_FIELDS) for row in tabs),
            panes=tuple(_safe_fields(row, PANE_FIELDS) for row in panes),
            agents=tuple(
                _safe_fields(row, AGENT_FIELDS)
                for row in agents
                if row.get("workspace_id") in workspace_ids
            ),
            worktrees=tuple(
                _safe_fields(row, WORKTREE_FIELDS)
                for row in worktrees
                if _path_is_within(row.get("path"), self.workspace)
            ),
        )

    def _base_rows(
        self,
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        agents = _rows(self._run(["herdr", "agent", "list"]), "agents")
        workspaces = _rows(self._run(["herdr", "workspace", "list"]), "workspaces")
        worktrees = _rows(
            self._run(
                [
                    "herdr",
                    "worktree",
                    "list",
                    "--cwd",
                    str(self.workspace),
                ]
            ),
            "worktrees",
        )
        return agents, workspaces, worktrees

    def _workspace_rows(
        self,
        workspace_ids: set[str],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        tabs: list[dict[str, object]] = []
        panes: list[dict[str, object]] = []
        for workspace_id in sorted(workspace_ids):
            tabs.extend(self._workspace_rows_for_id(workspace_id, "tab", "tabs"))
            panes.extend(self._workspace_rows_for_id(workspace_id, "pane", "panes"))
        return tabs, panes

    def _workspace_rows_for_id(
        self,
        workspace_id: str,
        resource: str,
        key: str,
    ) -> list[dict[str, object]]:
        rows = _rows(
            self._run(
                [
                    "herdr",
                    resource,
                    "list",
                    "--workspace",
                    workspace_id,
                ]
            ),
            key,
        )
        return [row for row in rows if row.get("workspace_id") == workspace_id]

    def _run(self, argv: list[str]) -> Mapping[str, Any]:
        return run_json(
            self.runner,
            Command(argv, self.workspace, CONTROL_TIMEOUT_SECONDS),
        )

    def _relevant_workspace_ids(
        self,
        workspaces: list[dict[str, object]],
        agents: list[dict[str, object]],
        worktrees: list[dict[str, object]],
    ) -> set[str]:
        workspace_ids: set[str] = set()
        for row in workspaces:
            workspace_id = row.get("workspace_id")
            worktree = row.get("worktree")
            if (
                _non_empty_string(workspace_id)
                and isinstance(worktree, dict)
                and (
                    _same_path(worktree.get("repo_root"), self.workspace)
                    or _path_is_within(worktree.get("checkout_path"), self.workspace)
                )
            ):
                workspace_ids.add(workspace_id)
        for row in worktrees:
            workspace_id = row.get("open_workspace_id")
            if _non_empty_string(workspace_id) and _path_is_within(row.get("path"), self.workspace):
                workspace_ids.add(workspace_id)
        for row in agents:
            workspace_id = row.get("workspace_id")
            if _non_empty_string(workspace_id) and _path_is_within(row.get("cwd"), self.workspace):
                workspace_ids.add(workspace_id)
        return workspace_ids


WORKSPACE_FIELDS = frozenset(
    {
        "workspace_id",
        "label",
        "focused",
        "agent_status",
        "tab_count",
        "pane_count",
        "active_tab_id",
    }
)
TAB_FIELDS = frozenset(
    {
        "workspace_id",
        "tab_id",
        "label",
        "focused",
        "agent_status",
        "pane_count",
    }
)
PANE_FIELDS = frozenset(
    {
        "workspace_id",
        "tab_id",
        "pane_id",
        "cwd",
        "focused",
        "agent",
        "agent_status",
        "interactive_ready",
    }
)
AGENT_FIELDS = frozenset(
    {
        "name",
        "agent",
        "agent_status",
        "interactive_ready",
        "state_change_seq",
        "workspace_id",
        "tab_id",
        "pane_id",
        "cwd",
        "focused",
    }
)
WORKTREE_FIELDS = frozenset(
    {
        "path",
        "branch",
        "label",
        "open_workspace_id",
        "is_linked_worktree",
        "is_detached",
        "is_prunable",
    }
)


def _scoped_panes(
    tabs: list[dict[str, object]],
    panes: list[dict[str, object]],
    workspace: Path,
) -> list[dict[str, object]]:
    tab_locations = {
        (row["workspace_id"], row["tab_id"])
        for row in tabs
        if _non_empty_string(row.get("workspace_id")) and _non_empty_string(row.get("tab_id"))
    }
    return [
        row
        for row in panes
        if (row.get("workspace_id"), row.get("tab_id")) in tab_locations
        and _path_is_within(row.get("cwd"), workspace)
    ]


def _scoped_agents(
    agents: list[dict[str, object]],
    workspace: Path,
) -> list[dict[str, object]]:
    return [row for row in agents if _path_is_within(row.get("cwd"), workspace)]


def _rows(result: Mapping[str, Any], key: str) -> list[dict[str, object]]:
    value = result.get(key)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise TransportError("herdr_invalid_response")
    return [dict(row) for row in value]


def _safe_fields(
    row: Mapping[str, object],
    allowed: frozenset[str],
) -> dict[str, object]:
    return {
        key: value
        for key, value in row.items()
        if key in allowed and (value is None or isinstance(value, (bool, float, int, str)))
    }


def _same_path(value: object, expected: Path) -> bool:
    resolved = _safe_resolve(value)
    return resolved == expected if resolved is not None else False


def _path_is_within(value: object, root: Path) -> bool:
    resolved = _safe_resolve(value)
    return resolved.is_relative_to(root) if resolved is not None else False


def _non_empty_string(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _safe_resolve(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    try:
        return Path(value).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
