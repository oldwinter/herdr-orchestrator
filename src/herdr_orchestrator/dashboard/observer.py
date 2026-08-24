from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
            f"file:{self.path}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        try:
            jobs = connection.execute(
                """
                SELECT id, title, harness, dedupe_key, placement, state, attempts,
                       max_attempts, available_at, lease_until, agent_name, error_code,
                       execution_path, herdr_workspace_id, receipt_kind, receipt_value,
                       agent_settled, task_verified, error_summary, created_at, updated_at
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
                       receipts.error_summary, receipts.observed_at
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
            agents = _rows(
                self._run(["herdr", "agent", "list"]),
                "agents",
            )
            workspaces = _rows(
                self._run(["herdr", "workspace", "list"]),
                "workspaces",
            )
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
            workspace_ids = self._relevant_workspace_ids(
                workspaces,
                agents,
                worktrees,
            )
            tabs: list[dict[str, object]] = []
            panes: list[dict[str, object]] = []
            for workspace_id in sorted(workspace_ids):
                tabs.extend(
                    _rows(
                        self._run(
                            [
                                "herdr",
                                "tab",
                                "list",
                                "--workspace",
                                workspace_id,
                            ]
                        ),
                        "tabs",
                    )
                )
                panes.extend(
                    _rows(
                        self._run(
                            [
                                "herdr",
                                "pane",
                                "list",
                                "--workspace",
                                workspace_id,
                            ]
                        ),
                        "panes",
                    )
                )
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
                isinstance(workspace_id, str)
                and isinstance(worktree, dict)
                and (
                    _same_path(worktree.get("repo_root"), self.workspace)
                    or _path_is_within(worktree.get("checkout_path"), self.workspace)
                )
            ):
                workspace_ids.add(workspace_id)
        for row in worktrees:
            workspace_id = row.get("open_workspace_id")
            if (
                isinstance(workspace_id, str)
                and _path_is_within(row.get("path"), self.workspace)
            ):
                workspace_ids.add(workspace_id)
        for row in agents:
            workspace_id = row.get("workspace_id")
            if (
                isinstance(workspace_id, str)
                and _path_is_within(row.get("cwd"), self.workspace)
            ):
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


def _rows(result: Mapping[str, Any], key: str) -> list[dict[str, object]]:
    value = result.get(key)
    if not isinstance(value, list):
        raise TransportError("herdr_invalid_response")
    return [dict(row) for row in value if isinstance(row, dict)]


def _safe_fields(
    row: Mapping[str, object],
    allowed: frozenset[str],
) -> dict[str, object]:
    return {key: value for key, value in row.items() if key in allowed}


def _same_path(value: object, expected: Path) -> bool:
    return isinstance(value, str) and Path(value).resolve() == expected


def _path_is_within(value: object, root: Path) -> bool:
    return isinstance(value, str) and Path(value).resolve().is_relative_to(root)
