from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from herdr_orchestrator.model import AgentState, DispatchOutcome, Harness
from herdr_orchestrator.protocol import (
    Command,
    CommandRunner,
    TransportError,
    run_json,
    run_text,
    subprocess_runner,
)

CONTROL_TIMEOUT_SECONDS = 10
START_TIMEOUT_MS = 120_000
START_RECOVERY_TIMEOUT_MS = 120_000
SHELL_READY_TIMEOUT_SECONDS = 10
SETTLED_STATES = {AgentState.IDLE, AgentState.DONE}


class HerdrTransport:
    def __init__(
        self,
        workflow_name: str,
        workspace: Path,
        *,
        environ: Mapping[str, str] | None = None,
        runner: CommandRunner = subprocess_runner,
    ) -> None:
        self.workflow_name = workflow_name
        self.workspace = workspace.resolve()
        self.environ = dict(os.environ if environ is None else environ)
        self.runner = runner
        self._provision_lock = threading.Lock()
        self._created_panes: dict[str, str] = {}
        self._created_tabs: dict[str, str] = {}

    def check_environment(self) -> None:
        if self.environ.get("HERDR_ENV") != "1":
            raise TransportError("not_in_herdr")
        if not self.environ.get("HERDR_PANE_ID"):
            raise TransportError("herdr_pane_id_missing")
        if not self.environ.get("HERDR_WORKSPACE_ID"):
            raise TransportError("herdr_workspace_id_missing")

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
    ) -> DispatchOutcome:
        name = agent_name or stable_agent_name(self.workflow_name, self.workspace, harness)
        try:
            self.check_environment()
            with self._provision_lock:
                pane_id, reused = self._ensure_agent(name, harness)
            state = self._prompt(name, prompt, timeout_seconds)
        except TransportError as exc:
            return DispatchOutcome(
                agent_name=name,
                state=AgentState.BLOCKED if exc.code == "agent_blocked" else AgentState.UNKNOWN,
                member_reused=name not in self._created_panes,
                pane_id=self._created_panes.get(name),
                error_code=exc.code,
            )
        return DispatchOutcome(name, state, reused, pane_id)

    def read_agent(self, name: str, *, lines: int = 120) -> str:
        return run_text(
            self.runner,
            Command(
                [
                    "herdr",
                    "agent",
                    "read",
                    name,
                    "--source",
                    "recent-unwrapped",
                    "--lines",
                    str(lines),
                ],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )

    def close_created_agent(self, name: str) -> None:
        self._created_panes.pop(name, None)
        tab_id = self._created_tabs.pop(name, None)
        if tab_id is None:
            return
        run_json(
            self.runner,
            Command(
                ["herdr", "tab", "close", tab_id],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )

    def _ensure_agent(self, name: str, harness: Harness) -> tuple[str | None, bool]:
        try:
            result = run_json(
                self.runner,
                Command(
                    ["herdr", "agent", "get", name],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
        except TransportError as exc:
            if exc.code != "agent_not_found":
                raise
        else:
            agent = _agent_payload(result)
            _validate_reusable_agent(agent, name, harness, self.workspace)
            pane_id = _non_empty_string(agent, "pane_id")
            return pane_id, True

        created = run_json(
            self.runner,
            Command(
                [
                    "herdr",
                    "tab",
                    "create",
                    "--workspace",
                    self.environ["HERDR_WORKSPACE_ID"],
                    "--cwd",
                    str(self.workspace),
                    "--label",
                    name,
                    "--no-focus",
                ],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )
        pane = created.get("root_pane")
        tab = created.get("tab")
        if not isinstance(pane, dict) or not isinstance(tab, dict):
            raise TransportError("herdr_invalid_response")
        pane_id = _non_empty_string(pane, "pane_id")
        tab_id = _non_empty_string(tab, "tab_id")
        try:
            self._wait_for_shell(pane_id)
            started = self._start_agent(name, harness, pane_id)
            _validate_started_agent(_agent_payload(started), name, harness, pane_id)
        except TransportError as cause:
            self._close_failed_tab(tab_id, cause)
            raise
        self._created_panes[name] = pane_id
        self._created_tabs[name] = tab_id
        return pane_id, False

    def _wait_for_shell(self, pane_id: str) -> None:
        deadline = time.monotonic() + SHELL_READY_TIMEOUT_SECONDS
        while True:
            result = run_json(
                self.runner,
                Command(
                    ["herdr", "pane", "process-info", "--pane", pane_id],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
            process_info = result.get("process_info")
            if isinstance(process_info, dict):
                shell_pid = process_info.get("shell_pid")
                processes = process_info.get("foreground_processes")
                if isinstance(shell_pid, int) and isinstance(processes, list):
                    for process in processes:
                        if isinstance(process, dict) and process.get("pid") == shell_pid:
                            return
            if time.monotonic() >= deadline:
                raise TransportError("pane_shell_not_ready")
            time.sleep(0.25)

    def _start_agent(
        self,
        name: str,
        harness: Harness,
        pane_id: str,
    ) -> Mapping[str, Any]:
        command = Command(
            [
                "herdr",
                "agent",
                "start",
                name,
                "--kind",
                harness.value,
                "--pane",
                pane_id,
                "--timeout",
                str(START_TIMEOUT_MS),
            ],
            self.workspace,
            START_TIMEOUT_MS // 1000 + 10,
        )
        for attempt in range(3):
            try:
                return run_json(self.runner, command)
            except TransportError as exc:
                if exc.code == "agent_pane_busy" and attempt < 2:
                    time.sleep(1)
                    continue
                if exc.code == "agent_not_ready":
                    return run_json(
                        self.runner,
                        Command(
                            [
                                "herdr",
                                "agent",
                                "wait",
                                name,
                                "--timeout",
                                str(START_RECOVERY_TIMEOUT_MS),
                            ],
                            self.workspace,
                            START_RECOVERY_TIMEOUT_MS // 1000 + 10,
                        ),
                    )
                raise
        raise TransportError("agent_start_failed")

    def _prompt(self, name: str, prompt: str, timeout_seconds: int) -> AgentState:
        before = _agent_payload(
            run_json(
                self.runner,
                Command(
                    ["herdr", "agent", "get", name],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
        )
        baseline_sequence = _state_change_sequence(before)
        deadline = time.monotonic() + timeout_seconds
        try:
            prompted = _agent_payload(
                run_json(
                    self.runner,
                    Command(
                        [
                            "herdr",
                            "agent",
                            "prompt",
                            name,
                            prompt,
                            "--wait",
                            "--timeout",
                            str(timeout_seconds * 1000),
                        ],
                        self.workspace,
                        timeout_seconds + 10,
                    ),
                )
            )
        except TransportError as exc:
            if exc.code != "agent_prompt_stalled":
                raise
            stalled = _agent_payload(
                run_json(
                    self.runner,
                    Command(
                        ["herdr", "agent", "get", name],
                        self.workspace,
                        CONTROL_TIMEOUT_SECONDS,
                    ),
                )
            )
            stalled_state = _agent_state(stalled)
            stalled_sequence = _state_change_sequence(stalled)
            if stalled_sequence > baseline_sequence and stalled_state in {
                AgentState.IDLE,
                AgentState.DONE,
                AgentState.BLOCKED,
            }:
                return stalled_state
            if stalled_sequence == baseline_sequence and stalled_state is AgentState.IDLE:
                run_json(
                    self.runner,
                    Command(
                        ["herdr", "agent", "send-keys", name, "enter"],
                        self.workspace,
                        CONTROL_TIMEOUT_SECONDS,
                    ),
                )
        else:
            state = _agent_state(prompted)
            if state in {
                AgentState.IDLE,
                AgentState.DONE,
                AgentState.BLOCKED,
            }:
                return state
            raise TransportError("agent_not_settled")

        while True:
            if time.monotonic() >= deadline:
                raise TransportError("herdr_timeout")
            current = _agent_payload(
                run_json(
                    self.runner,
                    Command(
                        ["herdr", "agent", "get", name],
                        self.workspace,
                        CONTROL_TIMEOUT_SECONDS,
                    ),
                )
            )
            state = _agent_state(current)
            sequence = _state_change_sequence(current)
            if sequence > baseline_sequence and state in {
                AgentState.IDLE,
                AgentState.DONE,
                AgentState.BLOCKED,
            }:
                return state
            time.sleep(0.5)

    def _close_failed_tab(self, tab_id: str, cause: TransportError) -> None:
        try:
            run_json(
                self.runner,
                Command(
                    ["herdr", "tab", "close", tab_id],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
        except TransportError as cleanup_error:
            raise TransportError("tab_cleanup_failed") from cause


def stable_agent_name(workflow_name: str, workspace: Path, harness: Harness) -> str:
    seed = f"{workflow_name}\0{workspace.resolve()}\0{harness.value}"
    digest = hashlib.sha256(seed.encode()).hexdigest()[:8]
    return f"ho-{harness.value}-{digest}"


def replica_slot_names(
    workflow_name: str,
    workspace: Path,
    harness: Harness,
    replicas: int,
) -> tuple[str, ...]:
    if replicas < 1:
        raise ValueError("replicas_must_be_positive")
    if replicas == 1:
        return (stable_agent_name(workflow_name, workspace, harness),)
    seed = f"{workflow_name}\0{workspace.resolve()}\0{harness.value}"
    digest = hashlib.sha256(seed.encode()).hexdigest()[:6]
    return tuple(
        f"ho-{harness.value}-{index:02d}-{digest}" for index in range(1, replicas + 1)
    )


def smoke_agent_name(workflow_name: str, harness: Harness) -> str:
    digest = hashlib.sha256(f"{workflow_name}\0smoke\0{harness.value}".encode()).hexdigest()[:6]
    return f"smoke-{harness.value}-{digest}"


def _agent_payload(result: Mapping[str, Any]) -> Mapping[str, Any]:
    agent = result.get("agent")
    if not isinstance(agent, dict):
        raise TransportError("herdr_invalid_response")
    return agent


def _agent_state(agent: Mapping[str, Any]) -> AgentState:
    value = agent.get("agent_status")
    if not isinstance(value, str):
        raise TransportError("herdr_invalid_response")
    try:
        return AgentState(value)
    except ValueError as exc:
        raise TransportError("herdr_invalid_response") from exc


def _state_change_sequence(agent: Mapping[str, Any]) -> int:
    value = agent.get("state_change_seq")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TransportError("herdr_invalid_response")
    return value


def _non_empty_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise TransportError("herdr_invalid_response")
    return value


def _validate_reusable_agent(
    agent: Mapping[str, Any],
    name: str,
    harness: Harness,
    workspace: Path,
) -> None:
    live_name = agent.get("name")
    if live_name is not None and live_name != name:
        raise TransportError("agent_identity_mismatch")
    if agent.get("agent") != harness.value:
        raise TransportError("agent_identity_mismatch")
    for key in ("cwd", "foreground_cwd"):
        value = agent.get(key)
        if not isinstance(value, str) or Path(value).resolve() != workspace:
            raise TransportError("agent_workspace_mismatch")
    state = _agent_state(agent)
    if state is AgentState.BLOCKED:
        raise TransportError("agent_blocked")
    if state not in SETTLED_STATES:
        raise TransportError("agent_not_settled")


def _validate_started_agent(
    agent: Mapping[str, Any],
    name: str,
    harness: Harness,
    pane_id: str,
) -> None:
    live_name = agent.get("name")
    if live_name is not None and live_name != name:
        raise TransportError("agent_identity_mismatch")
    if agent.get("agent") != harness.value or agent.get("pane_id") != pane_id:
        raise TransportError("agent_identity_mismatch")
    if _agent_state(agent) not in SETTLED_STATES:
        raise TransportError("agent_not_settled")
