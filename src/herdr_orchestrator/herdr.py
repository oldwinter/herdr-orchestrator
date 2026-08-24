from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from herdr_orchestrator.herdr_layout import HerdrLayout, ProvisionedTerminal
from herdr_orchestrator.model import (
    AgentState,
    DispatchContext,
    DispatchOutcome,
    Harness,
    PlacementTarget,
)
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
AGENT_POST_START_SETTLE_SECONDS = 3
AGENT_INTERACTIVE_READY_TIMEOUT_SECONDS = 10
PROMPT_ACCEPTANCE_TIMEOUT_MS = 5_000
PROMPT_ENTER_RETRIES = 2
SETTLED_CONFIRMATION_POLLS = 6
SETTLED_STATES = {AgentState.IDLE, AgentState.DONE}
CLAUDE_AUTH_FAILURE = re.compile(
    r"(?im)^\s*⏺\s+Please run /login\b.*\bAPI Error:\s*(?:401|403)\b"
)


class HerdrTransport:
    def __init__(
        self,
        workflow_name: str,
        workspace: Path,
        *,
        environ: Mapping[str, str] | None = None,
        runner: CommandRunner = subprocess_runner,
        sleeper: Callable[[float], None] = time.sleep,
        settled_confirmation_polls: int = SETTLED_CONFIRMATION_POLLS,
        inspect_runtime_errors: bool = True,
    ) -> None:
        self.workflow_name = workflow_name
        self.workspace = workspace.resolve()
        self.environ = dict(os.environ if environ is None else environ)
        self.runner = runner
        self.sleeper = sleeper
        self.settled_confirmation_polls = settled_confirmation_polls
        self.inspect_runtime_errors = inspect_runtime_errors
        self._provision_lock = threading.Lock()
        self._created_terminals: dict[str, ProvisionedTerminal] = {}
        self._layout = HerdrLayout(
            workflow_name,
            self.workspace,
            self.environ.get("HERDR_WORKSPACE_ID", ""),
            runner,
        )

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
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        refresh_visible_label = context is not None
        dispatch_context = context or DispatchContext(
            PlacementTarget.TAB,
            harness.value,
            agent_name or harness.value,
        )
        name = agent_name or stable_agent_name(self.workflow_name, self.workspace, harness)
        pane_id: str | None = None
        workspace_id: str | None = None
        try:
            self.check_environment()
            with self._provision_lock:
                pane_id, reused, workspace_id = self._ensure_agent(
                    name,
                    harness,
                    dispatch_context,
                    refresh_visible_label=refresh_visible_label,
                )
            state = self._prompt(name, harness, prompt, timeout_seconds)
        except TransportError as exc:
            terminal = self._created_terminals.get(name)
            return DispatchOutcome(
                agent_name=name,
                state=AgentState.BLOCKED if exc.code == "agent_blocked" else AgentState.UNKNOWN,
                member_reused=terminal is None,
                pane_id=terminal.pane_id if terminal is not None else pane_id,
                error_code=exc.code,
                placement=dispatch_context.placement,
                execution_path=str(
                    terminal.cwd
                    if terminal is not None
                    else self._layout.execution_workspace(dispatch_context)
                ),
                herdr_workspace_id=(
                    terminal.workspace_id if terminal is not None else workspace_id
                ),
            )
        return DispatchOutcome(
            name,
            state,
            reused,
            pane_id,
            placement=dispatch_context.placement,
            execution_path=str(self._layout.execution_workspace(dispatch_context)),
            herdr_workspace_id=workspace_id,
        )

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

    def respond(
        self,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        try:
            self.check_environment()
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
            if _agent_state(current) is not AgentState.BLOCKED:
                raise TransportError("agent_not_blocked")
            pane_id = _non_empty_string(current, "pane_id")
            baseline_sequence = _state_change_sequence(current)
            run_json(
                self.runner,
                Command(
                    ["herdr", "pane", "send-text", pane_id, response],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
            run_json(
                self.runner,
                Command(
                    ["herdr", "agent", "send-keys", name, "enter"],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
            state = self._wait_after_blocked_response(
                name,
                harness,
                baseline_sequence,
                timeout_seconds,
            )
        except TransportError as exc:
            return DispatchOutcome(
                agent_name=name,
                state=AgentState.BLOCKED if exc.code == "agent_blocked" else AgentState.UNKNOWN,
                member_reused=True,
                pane_id=None,
                error_code=exc.code,
            )
        return DispatchOutcome(name, state, True, pane_id)

    def _wait_after_blocked_response(
        self,
        name: str,
        harness: Harness,
        baseline_sequence: int,
        timeout_seconds: int,
    ) -> AgentState:
        deadline = time.monotonic() + timeout_seconds
        while True:
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
                state = self._confirm_stable_settlement(name, state, deadline)
                self._raise_for_runtime_error(name, harness, state)
                return state
            if time.monotonic() >= deadline:
                raise TransportError("herdr_timeout")
            self.sleeper(0.5)

    def close_created_agent(self, name: str) -> None:
        terminal = self._created_terminals.pop(name, None)
        if terminal is None:
            return
        self._layout.close_temporary(terminal)

    def _ensure_agent(
        self,
        name: str,
        harness: Harness,
        context: DispatchContext,
        *,
        refresh_visible_label: bool,
    ) -> tuple[str | None, bool, str | None]:
        execution_workspace = self._layout.execution_workspace(context)
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
            _validate_reusable_agent(agent, name, harness, execution_workspace)
            if refresh_visible_label:
                self._layout.refresh_visible_label(agent, context)
            pane_id = _non_empty_string(agent, "pane_id")
            raw_workspace_id = agent.get("workspace_id")
            workspace_id = (
                raw_workspace_id.strip()
                if isinstance(raw_workspace_id, str) and raw_workspace_id.strip()
                else (
                    self._layout.workspace_id
                    if context.placement is not PlacementTarget.WORKTREE
                    else None
                )
            )
            return pane_id, True, workspace_id

        terminal = self._layout.provision(context)
        pane_id = terminal.pane_id
        try:
            self._wait_for_shell(pane_id)
            started = self._start_agent(name, harness, pane_id)
            started_agent = _agent_payload(started)
            started_state = _agent_state(started_agent)
            if started_state not in SETTLED_STATES | {AgentState.BLOCKED}:
                started = run_json(
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
            self.sleeper(AGENT_POST_START_SETTLE_SECONDS)
            started_agent = self._wait_for_interactive_agent(name, harness, pane_id)
            _validate_started_agent(started_agent, name, harness, pane_id)
        except TransportError as cause:
            if cause.code == "agent_blocked":
                self._created_terminals[name] = terminal
                raise
            try:
                self._layout.cleanup_failed(terminal)
            except TransportError as cleanup_error:
                raise TransportError("layout_cleanup_failed") from cause
            raise
        self._created_terminals[name] = terminal
        return pane_id, False, terminal.workspace_id

    def _wait_for_interactive_agent(
        self,
        name: str,
        harness: Harness,
        pane_id: str,
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + AGENT_INTERACTIVE_READY_TIMEOUT_SECONDS
        while True:
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
            _validate_agent_identity(current, name, harness, pane_id)
            state = _agent_state(current)
            if _interactive_ready(current):
                if state is AgentState.BLOCKED:
                    raise TransportError("agent_blocked")
                if state in SETTLED_STATES:
                    return current
            if time.monotonic() >= deadline:
                if state is AgentState.BLOCKED:
                    raise TransportError("agent_blocked")
                raise TransportError("agent_not_ready")
            self.sleeper(0.5)

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
            self.sleeper(0.25)

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
                    self.sleeper(1)
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

    def _prompt(
        self,
        name: str,
        harness: Harness,
        prompt: str,
        timeout_seconds: int,
    ) -> AgentState:
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
        acceptance_timeout_ms = min(timeout_seconds * 1000, PROMPT_ACCEPTANCE_TIMEOUT_MS)
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
                            "--until",
                            AgentState.WORKING.value,
                            "--until",
                            AgentState.IDLE.value,
                            "--until",
                            AgentState.DONE.value,
                            "--until",
                            AgentState.BLOCKED.value,
                            "--timeout",
                            str(acceptance_timeout_ms),
                        ],
                        self.workspace,
                        acceptance_timeout_ms // 1000 + 10,
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
            state = _agent_state(stalled)
            sequence = _state_change_sequence(stalled)
            if sequence == baseline_sequence and state is AgentState.IDLE:
                stalled = self._resubmit_enter_until_turn(name, baseline_sequence)
                state = _agent_state(stalled)
                sequence = _state_change_sequence(stalled)
            if sequence <= baseline_sequence:
                raise TransportError("agent_turn_not_observed")
            if state in {
                AgentState.IDLE,
                AgentState.DONE,
                AgentState.BLOCKED,
            }:
                state = self._confirm_stable_settlement(name, state, deadline)
                self._raise_for_runtime_error(name, harness, state)
                return state
            if state is not AgentState.WORKING:
                raise TransportError("agent_not_settled")
        else:
            state = _agent_state(prompted)
            sequence = _state_change_sequence(prompted)
            if sequence <= baseline_sequence:
                raise TransportError("agent_turn_not_observed")
            if state in {
                AgentState.IDLE,
                AgentState.DONE,
                AgentState.BLOCKED,
            }:
                state = self._confirm_stable_settlement(name, state, deadline)
                self._raise_for_runtime_error(name, harness, state)
                return state
            if state is not AgentState.WORKING:
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
                state = self._confirm_stable_settlement(name, state, deadline)
                self._raise_for_runtime_error(name, harness, state)
                return state
            self.sleeper(0.5)

    def _resubmit_enter_until_turn(
        self,
        name: str,
        baseline_sequence: int,
    ) -> Mapping[str, Any]:
        for _ in range(PROMPT_ENTER_RETRIES):
            run_json(
                self.runner,
                Command(
                    ["herdr", "agent", "send-keys", name, "enter"],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
            self.sleeper(0.5)
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
            if _state_change_sequence(current) > baseline_sequence:
                return current
        raise TransportError("agent_turn_not_observed")

    def _confirm_stable_settlement(
        self,
        name: str,
        state: AgentState,
        deadline: float,
    ) -> AgentState:
        if state is AgentState.BLOCKED:
            return state

        confirmations = 0
        while confirmations < self.settled_confirmation_polls:
            if time.monotonic() >= deadline:
                raise TransportError("herdr_timeout")
            self.sleeper(0.5)
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
            if state is AgentState.BLOCKED:
                return state
            if state in SETTLED_STATES:
                confirmations += 1
            else:
                confirmations = 0
        return state

    def _raise_for_runtime_error(
        self,
        name: str,
        harness: Harness,
        state: AgentState,
    ) -> None:
        if not self.inspect_runtime_errors or state not in SETTLED_STATES:
            return
        output = run_text(
            self.runner,
            Command(
                [
                    "herdr",
                    "agent",
                    "read",
                    name,
                    "--source",
                    "detection",
                    "--lines",
                    "80",
                ],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )
        if harness is Harness.CLAUDE and CLAUDE_AUTH_FAILURE.search(output):
            raise TransportError("agent_auth_failed")

def stable_agent_name(workflow_name: str, workspace: Path, harness: Harness) -> str:
    seed = f"{workflow_name}\0{workspace.resolve()}\0{harness.value}"
    digest = hashlib.sha256(seed.encode()).hexdigest()[:8]
    return f"ho-{harness.value}-{digest}"


def replica_slot_names(
    workflow_name: str,
    workspace: Path,
    harness: Harness,
    replicas: int,
    placement: PlacementTarget = PlacementTarget.TAB,
) -> tuple[str, ...]:
    if replicas < 1:
        raise ValueError("replicas_must_be_positive")
    if placement is PlacementTarget.WORKTREE:
        raise ValueError("worktree_slots_are_task_scoped")
    target = "" if placement is PlacementTarget.TAB else f"-{placement.value}"
    if replicas == 1:
        if placement is PlacementTarget.TAB:
            return (stable_agent_name(workflow_name, workspace, harness),)
        seed = (
            f"{workflow_name}\0{workspace.resolve()}\0{harness.value}"
            f"\0{placement.value}"
        )
        digest = hashlib.sha256(seed.encode()).hexdigest()[:6]
        return (f"ho-{harness.value}{target}-{digest}"[:32],)
    seed = (
        f"{workflow_name}\0{workspace.resolve()}\0{harness.value}"
        f"\0{placement.value}"
    )
    digest = hashlib.sha256(seed.encode()).hexdigest()[:6]
    return tuple(
        f"ho-{harness.value}{target}-{index:02d}-{digest}"[:32]
        for index in range(1, replicas + 1)
    )


def worktree_agent_name(
    workflow_name: str,
    harness: Harness,
    job_id: int,
) -> str:
    digest = hashlib.sha256(f"{workflow_name}\0worktree\0{job_id}".encode()).hexdigest()[:6]
    return f"ho-{harness.value}-wt-{job_id}-{digest}"[:32].rstrip("-")


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


def _interactive_ready(agent: Mapping[str, Any]) -> bool:
    value = agent.get("interactive_ready")
    if not isinstance(value, bool):
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
    if not _interactive_ready(agent):
        raise TransportError("agent_not_ready")
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
    _validate_agent_identity(agent, name, harness, pane_id)
    if not _interactive_ready(agent):
        raise TransportError("agent_not_ready")
    state = _agent_state(agent)
    if state is AgentState.BLOCKED:
        raise TransportError("agent_blocked")
    if state not in SETTLED_STATES:
        raise TransportError("agent_not_settled")


def _validate_agent_identity(
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
