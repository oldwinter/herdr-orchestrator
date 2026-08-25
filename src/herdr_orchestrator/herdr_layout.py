from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from herdr_orchestrator.model import DispatchContext, PlacementTarget
from herdr_orchestrator.protocol import Command, CommandRunner, TransportError, run_json
from herdr_orchestrator.topology import short_display_label, stable_slug

CONTROL_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class ProvisionedTerminal:
    pane_id: str
    tab_id: str
    workspace_id: str
    cwd: Path
    placement: PlacementTarget
    cleanup_kind: str
    cleanup_id: str


@dataclass(slots=True)
class _BatchTab:
    tab_id: str
    workspace_id: str
    pane_ids: list[str]


class HerdrLayout:
    def __init__(
        self,
        workflow_name: str,
        workspace: Path,
        workspace_id: str,
        runner: CommandRunner,
    ) -> None:
        self.workflow_name = workflow_name
        self.workspace = workspace.resolve()
        self.workspace_id = workspace_id
        self.runner = runner
        self._batch_tabs: dict[str, _BatchTab] = {}

    def execution_workspace(self, context: DispatchContext) -> Path:
        if context.placement is not PlacementTarget.WORKTREE:
            return self.workspace
        return self._worktree_coordinates(context)[0]

    def provision(self, context: DispatchContext) -> ProvisionedTerminal:
        match context.placement:
            case PlacementTarget.TAB:
                return self._create_tab(context)
            case PlacementTarget.PANE:
                return self._create_batch_pane(context)
            case PlacementTarget.WORKTREE:
                return self._create_or_open_worktree(context)
        raise TransportError("placement_unsupported")

    def refresh_visible_label(
        self,
        agent: Mapping[str, Any],
        context: DispatchContext,
    ) -> None:
        label = short_display_label(context.title, fallback="task")
        if context.placement is PlacementTarget.TAB:
            command = [
                "herdr",
                "tab",
                "rename",
                _non_empty_string(agent, "tab_id"),
                label,
            ]
        else:
            return
        run_json(
            self.runner,
            Command(command, self.workspace, CONTROL_TIMEOUT_SECONDS),
        )

    def cleanup_failed(self, terminal: ProvisionedTerminal) -> None:
        if terminal.cleanup_kind == "tab":
            self._forget_batch_terminal(terminal)
            self._close_tab(terminal.cleanup_id)
            return
        if terminal.cleanup_kind == "pane":
            self._forget_batch_terminal(terminal)
            run_json(
                self.runner,
                Command(
                    ["herdr", "pane", "close", terminal.cleanup_id],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
            return
        # Worktrees are durable task evidence. Never auto-remove their workspace,
        # checkout, or branch after a failed dispatch.

    def close_temporary(self, terminal: ProvisionedTerminal) -> None:
        if terminal.placement is PlacementTarget.WORKTREE:
            return
        if terminal.cleanup_kind == "tab":
            self._forget_batch_terminal(terminal)
            self._close_tab(terminal.cleanup_id)
        elif terminal.cleanup_kind == "pane":
            self._forget_batch_terminal(terminal)
            run_json(
                self.runner,
                Command(
                    ["herdr", "pane", "close", terminal.cleanup_id],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )

    def _create_tab(self, context: DispatchContext) -> ProvisionedTerminal:
        result = run_json(
            self.runner,
            Command(
                [
                    "herdr",
                    "tab",
                    "create",
                    "--workspace",
                    self.workspace_id,
                    "--cwd",
                    str(self.workspace),
                    "--label",
                    short_display_label(context.title, fallback="task"),
                    "--no-focus",
                ],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )
        pane_id, tab_id, workspace_id = _created_layout(
            result,
            fallback_workspace_id=self.workspace_id,
        )
        return ProvisionedTerminal(
            pane_id,
            tab_id,
            workspace_id,
            self.workspace,
            PlacementTarget.TAB,
            "tab",
            tab_id,
        )

    def _create_batch_pane(self, context: DispatchContext) -> ProvisionedTerminal:
        batch_key = context.batch_key
        if not batch_key:
            raise TransportError("placement_batch_key_missing")
        batch = self._batch_tabs.get(batch_key)
        if batch is None:
            result = run_json(
                self.runner,
                Command(
                    [
                        "herdr",
                        "tab",
                        "create",
                        "--workspace",
                        self.workspace_id,
                        "--cwd",
                        str(self.workspace),
                        "--label",
                        short_display_label(
                            f"{self.workflow_name} run",
                            fallback="workflow run",
                        ),
                        "--no-focus",
                    ],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
            pane_id, tab_id, workspace_id = _created_layout(
                result,
                fallback_workspace_id=self.workspace_id,
            )
            batch = _BatchTab(tab_id, workspace_id, [pane_id])
            self._batch_tabs[batch_key] = batch
            return ProvisionedTerminal(
                pane_id,
                tab_id,
                workspace_id,
                self.workspace,
                PlacementTarget.PANE,
                "tab",
                tab_id,
            )

        target, direction = self._split_target(batch.pane_ids[0])
        result = run_json(
            self.runner,
            Command(
                [
                    "herdr",
                    "pane",
                    "split",
                    "--pane",
                    target,
                    "--direction",
                    direction,
                    "--cwd",
                    str(self.workspace),
                    "--no-focus",
                ],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )
        pane = result.get("pane")
        if not isinstance(pane, dict):
            raise TransportError("herdr_invalid_response")
        pane_id = _non_empty_string(pane, "pane_id")
        batch.pane_ids.append(pane_id)
        return ProvisionedTerminal(
            pane_id,
            batch.tab_id,
            batch.workspace_id,
            self.workspace,
            PlacementTarget.PANE,
            "pane",
            pane_id,
        )

    def _split_target(self, known_pane: str) -> tuple[str, str]:
        result = run_json(
            self.runner,
            Command(
                ["herdr", "pane", "layout", "--pane", known_pane],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )
        layout = result.get("layout")
        panes = layout.get("panes") if isinstance(layout, dict) else None
        if not isinstance(panes, list) or not panes:
            raise TransportError("herdr_invalid_response")
        candidates: list[tuple[int, str, int, int]] = []
        for pane in panes:
            if not isinstance(pane, dict):
                continue
            pane_id = pane.get("pane_id")
            rect = pane.get("rect")
            if (
                not isinstance(pane_id, str)
                or not isinstance(rect, dict)
                or not isinstance(rect.get("width"), int)
                or not isinstance(rect.get("height"), int)
            ):
                continue
            width = int(rect["width"])
            height = int(rect["height"])
            candidates.append((width * height, pane_id, width, height))
        if not candidates:
            raise TransportError("herdr_invalid_response")
        _, pane_id, width, height = max(candidates)
        direction = "right" if width >= height * 2 else "down"
        return pane_id, direction

    def _create_or_open_worktree(
        self,
        context: DispatchContext,
    ) -> ProvisionedTerminal:
        path, branch = self._worktree_coordinates(context)
        label = short_display_label(context.title, fallback="worktree")
        if path.exists():
            existing = self._open_worktree_terminal(path)
            if existing is not None:
                return existing
            command = [
                "herdr",
                "worktree",
                "open",
                "--cwd",
                str(self.workspace),
                "--path",
                str(path),
                "--label",
                label,
                "--no-focus",
            ]
        else:
            command = [
                "herdr",
                "worktree",
                "create",
                "--cwd",
                str(self.workspace),
                "--branch",
                branch,
                "--base",
                "HEAD",
                "--path",
                str(path),
                "--label",
                label,
                "--no-focus",
            ]
        result = run_json(
            self.runner,
            Command(command, self.workspace, 130),
        )
        pane_id, tab_id, workspace_id = _created_layout(result)
        return ProvisionedTerminal(
            pane_id,
            tab_id,
            workspace_id,
            path,
            PlacementTarget.WORKTREE,
            "worktree",
            workspace_id,
        )

    def _open_worktree_terminal(self, path: Path) -> ProvisionedTerminal | None:
        listed = run_json(
            self.runner,
            Command(
                ["herdr", "worktree", "list", "--cwd", str(self.workspace)],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )
        worktrees = listed.get("worktrees")
        if not isinstance(worktrees, list):
            raise TransportError("herdr_invalid_response")
        workspace_id = next(
            (
                row.get("open_workspace_id")
                for row in worktrees
                if isinstance(row, dict)
                and row.get("path") == str(path)
                and isinstance(row.get("open_workspace_id"), str)
            ),
            None,
        )
        if workspace_id is None:
            return None
        panes = run_json(
            self.runner,
            Command(
                ["herdr", "pane", "list", "--workspace", workspace_id],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        ).get("panes")
        if not isinstance(panes, list):
            raise TransportError("herdr_invalid_response")
        pane = next(
            (row for row in panes if isinstance(row, dict) and row.get("cwd") == str(path)),
            None,
        )
        if not isinstance(pane, dict):
            return None
        return ProvisionedTerminal(
            _non_empty_string(pane, "pane_id"),
            _non_empty_string(pane, "tab_id"),
            workspace_id,
            path,
            PlacementTarget.WORKTREE,
            "worktree",
            workspace_id,
        )

    def _worktree_coordinates(self, context: DispatchContext) -> tuple[Path, str]:
        if context.worktree_root is None:
            raise TransportError("placement_worktree_root_missing")
        digest = hashlib.sha256(f"{self.workflow_name}\0{context.task_key}".encode()).hexdigest()[
            :7
        ]
        slug = stable_slug(context.title, maximum=32)
        identifier = f"{slug}-{digest}"
        path = (
            context.worktree_root.resolve()
            / stable_slug(self.workflow_name, maximum=32)
            / identifier
        )
        branch = f"ho/{stable_slug(self.workflow_name)}/{identifier}"
        return path, branch

    def _close_tab(self, tab_id: str) -> None:
        run_json(
            self.runner,
            Command(
                ["herdr", "tab", "close", tab_id],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )

    def _forget_batch_terminal(self, terminal: ProvisionedTerminal) -> None:
        for key, batch in tuple(self._batch_tabs.items()):
            if batch.tab_id == terminal.tab_id:
                if terminal.cleanup_kind == "tab":
                    del self._batch_tabs[key]
                elif terminal.pane_id in batch.pane_ids:
                    batch.pane_ids.remove(terminal.pane_id)


def _created_layout(
    result: Mapping[str, Any],
    *,
    fallback_workspace_id: str | None = None,
) -> tuple[str, str, str]:
    pane = result.get("root_pane")
    tab = result.get("tab")
    workspace = result.get("workspace")
    if not isinstance(pane, dict) or not isinstance(tab, dict):
        raise TransportError("herdr_invalid_response")
    workspace_id = (
        _non_empty_string(workspace, "workspace_id")
        if isinstance(workspace, dict)
        else fallback_workspace_id
    )
    if not workspace_id:
        raise TransportError("herdr_invalid_response")
    return (
        _non_empty_string(pane, "pane_id"),
        _non_empty_string(tab, "tab_id"),
        workspace_id,
    )


def _non_empty_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise TransportError("herdr_invalid_response")
    return value
