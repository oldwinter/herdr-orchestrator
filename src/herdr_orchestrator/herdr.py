from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from herdr_orchestrator import attempt_runtime
from herdr_orchestrator.completion import (
    CompletionPolicy,
    FileReceiptSnapshot,
    completion_policy_for,
    failed_completion,
    file_receipt_snapshot,
    receipt_file_path,
    snapshot_file_receipt,
    snapshot_output_receipt,
    verify_completion,
    verify_legacy_receipt,
)
from herdr_orchestrator.herdr_layout import HerdrLayout, ProvisionedTerminal
from herdr_orchestrator.model import (
    AgentState,
    AttemptRuntime,
    DispatchContext,
    DispatchOutcome,
    Harness,
    PlacementTarget,
    TaskReceipt,
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
AGENT_INTERACTIVE_READY_TIMEOUT_SECONDS = 10
PROMPT_COMMAND_GRACE_SECONDS = 1
PROMPT_ENTER_RETRIES = 2
SETTLED_CONFIRMATION_POLLS = 6
SETTLED_STATES = {AgentState.IDLE, AgentState.DONE}
PROMPT_TIMEOUT_ERRORS = {"herdr_timeout", "timeout"}
CLAUDE_WORKSPACE_TRUST_MARKERS = (
    "Accessing workspace:",
    "Quick safety check:",
    "Yes, I trust this folder",
)
CLAUDE_AUTH_FAILURE = re.compile(r"(?im)^\s*⏺\s+Please run /login\b.*\bAPI Error:\s*(?:401|403)\b")
INVALID_MODEL_FAILURE = re.compile(
    r"(?is)(?:"
    r"ValidationException\b.{0,240}\bmodel\s+identifier\b.{0,120}\binvalid\b"
    r"|\binvalid\b.{0,120}\bmodel\s+(?:identifier|id)\b"
    r"|\b(?:unknown|unsupported)\s+model\b"
    r"|\bmodel\b.{0,120}\bnot\s+found\b"
    r")"
)
PROVIDER_RETRY_FAILURE = re.compile(
    r"(?im)^\s*(?:API|provider)\s+failed\s+after\s+\d+\s+(?:retries|attempts)\b"
)
AUTH_REQUIRED_FAILURE = re.compile(
    r"(?im)(?:"
    r"^\s*(?:factory\s+)?device\s+login\b"
    r"|^\s*(?:login|authentication|sign[ -]?in)\s+required\b"
    r"|^\s*not\s+(?:logged|signed)\s+in\b"
    r"|^\s*please\s+(?:run\s+)?/login\b"
    r"|^\s*waiting\s+for\s+(?:device\s+)?authentication\b"
    r")"
)
AUTH_FAILURE = re.compile(
    r"(?im)(?:"
    r"\bAPI\s+Error:\s*(?:401|403)\b"
    r"|^\s*(?:Error:\s*)?(?:HTTP\s+)?(?:401\s+Unauthorized|403\s+Forbidden|(?:401|403)\s+status\s+code)\b"
    r"|^\s*(?:Authentication|Authorization)\s+failed\b"
    r"|^\s*(?:Error:\s*)?(?:invalid|expired)\s+"
    r"(?:API[ -]?key|access\s+token|credentials?)\b"
    r")"
)

MAXIMUM_AUTOMATION_ARGUMENTS: dict[Harness, tuple[str, ...]] = {
    Harness.DROID: ("--auto", "high"),
    Harness.GROK: ("--always-approve", "--permission-mode", "bypassPermissions"),
    Harness.CODEX: (
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
    ),
    Harness.PI: ("--approve",),
    Harness.CLAUDE: ("--dangerously-skip-permissions",),
    Harness.HERMES: ("--yolo", "--accept-hooks"),
}


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
        self._dispatch_deadline = threading.local()
        self._created_terminals: dict[str, ProvisionedTerminal] = {}
        self._raw_runner = runner
        self.runner = self._bounded_runner
        self._layout = HerdrLayout(
            workflow_name,
            self.workspace,
            self.environ.get("HERDR_WORKSPACE_ID", ""),
            self.runner,
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
        timeout_seconds: float,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        previous = getattr(self._dispatch_deadline, "value", None)
        self._dispatch_deadline.value = time.monotonic() + timeout_seconds
        try:
            return self._dispatch_with_active_deadline(
                harness,
                prompt,
                timeout_seconds=timeout_seconds,
                agent_name=agent_name,
                context=context,
            )
        finally:
            if previous is None:
                del self._dispatch_deadline.value
            else:
                self._dispatch_deadline.value = previous

    def _dispatch_with_active_deadline(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        refresh_visible_label = context is not None
        dispatch_context = context or DispatchContext(
            PlacementTarget.TAB,
            harness.value,
            agent_name or harness.value,
        )
        completion_policy = completion_policy_for(
            dispatch_context.receipt,
            dispatch_context.completion_identity,
        )
        name = agent_name or stable_agent_name(self.workflow_name, self.workspace, harness)
        pane_id: str | None = None
        workspace_id: str | None = None
        state: AgentState | None = None
        reused = False
        dispatch_started = time.monotonic()
        phase_timings_ms: dict[str, int] = {}
        try:
            self.check_environment()
            provision_started = time.monotonic()
            acquired = self._provision_lock.acquire(
                timeout=self._remaining_seconds(timeout_seconds)
            )
            if not acquired:
                raise TransportError("herdr_timeout")
            try:
                pane_id, reused, workspace_id = self._ensure_agent(
                    name,
                    harness,
                    dispatch_context,
                    refresh_visible_label=refresh_visible_label,
                )
            finally:
                self._provision_lock.release()
            phase_timings_ms["provision_ready"] = attempt_runtime.elapsed_ms(provision_started)
            execution_path = str(self._layout.execution_workspace(dispatch_context))
            reporter = attempt_runtime.AttemptReporter(
                dispatch_context,
                name,
                pane_id,
                workspace_id,
                execution_path,
                reused,
            )
            receipt_baseline_started = time.monotonic()
            receipt_output_before = snapshot_output_receipt(
                dispatch_context.receipt,
                dispatch_context.completion_identity,
                lambda: self._read_receipt_output(name),
            )
            receipt_file_before = snapshot_file_receipt(
                dispatch_context.receipt,
                self._layout.execution_workspace(dispatch_context),
            )
            phase_timings_ms["receipt_baseline"] = attempt_runtime.elapsed_ms(
                receipt_baseline_started
            )
            turn_started = time.monotonic()
            try:
                settled = self._prompt(
                    name,
                    harness,
                    prompt,
                    timeout_seconds,
                    reporter=reporter,
                )
                state = _agent_state(settled)
            finally:
                phase_timings_ms["turn_settlement"] = attempt_runtime.elapsed_ms(turn_started)
            reporter.settled(state, sequence=_state_change_sequence(settled))
            self._raise_for_runtime_error(name, harness, state)
            if state is AgentState.BLOCKED:
                raise TransportError("agent_blocked")
            receipt_verification_started = time.monotonic()
            try:
                if (
                    completion_policy is not CompletionPolicy.LEGACY_UNVERIFIED
                    and state not in SETTLED_STATES
                ):
                    completion = failed_completion(completion_policy, "agent_not_settled")
                else:
                    completion = verify_completion(
                        dispatch_context.receipt,
                        dispatch_context.completion_identity,
                        self._layout.execution_workspace(dispatch_context),
                        prompt=prompt,
                        output_before=receipt_output_before,
                        file_before=receipt_file_before,
                        read_output=lambda: self._read_receipt_output(name),
                    )
            finally:
                phase_timings_ms["receipt_verification"] = attempt_runtime.elapsed_ms(
                    receipt_verification_started
                )
            reporter.receipt_observed(
                state,
                completion.task_verified,
                completion=completion,
            )
        except TransportError as exc:
            completion = failed_completion(completion_policy, exc.code)
            phase_timings_ms["total"] = attempt_runtime.elapsed_ms(dispatch_started)
            terminal = self._created_terminals.get(name)
            return DispatchOutcome(
                agent_name=name,
                state=AgentState.BLOCKED if exc.code == "agent_blocked" else AgentState.UNKNOWN,
                member_reused=reused,
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
                task_verified=completion.task_verified,
                error_summary=exc.summary,
                agent_settled=(exc.agent_settled or state in SETTLED_STATES),
                phase_timings_ms=phase_timings_ms,
                completion=completion,
            )
        phase_timings_ms["total"] = attempt_runtime.elapsed_ms(dispatch_started)
        return DispatchOutcome(
            name,
            state,
            reused,
            pane_id,
            placement=dispatch_context.placement,
            execution_path=str(self._layout.execution_workspace(dispatch_context)),
            herdr_workspace_id=workspace_id,
            error_code=completion.error_code,
            task_verified=completion.task_verified,
            agent_settled=state in SETTLED_STATES,
            phase_timings_ms=phase_timings_ms,
            completion=completion,
        )

    def recover(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str,
        context: DispatchContext,
        runtime: AttemptRuntime,
    ) -> DispatchOutcome:
        del prompt
        previous = getattr(self._dispatch_deadline, "value", None)
        self._dispatch_deadline.value = time.monotonic() + timeout_seconds
        try:
            return attempt_runtime.recover_turn(
                self,
                harness,
                timeout_seconds=timeout_seconds,
                agent_name=agent_name,
                context=context,
                runtime=runtime,
            )
        finally:
            if previous is None:
                del self._dispatch_deadline.value
            else:
                self._dispatch_deadline.value = previous

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
        expected_pane_id: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        previous = getattr(self._dispatch_deadline, "value", None)
        self._dispatch_deadline.value = time.monotonic() + timeout_seconds
        try:
            return self._respond_with_active_deadline(
                name,
                harness,
                response,
                timeout_seconds=timeout_seconds,
                expected_pane_id=expected_pane_id,
                context=context,
            )
        finally:
            if previous is None:
                del self._dispatch_deadline.value
            else:
                self._dispatch_deadline.value = previous

    def _respond_with_active_deadline(
        self,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
        expected_pane_id: str | None,
        context: DispatchContext | None,
    ) -> DispatchOutcome:
        pane_id = expected_pane_id
        workspace_id: str | None = None
        receipt = context.receipt if context is not None else None
        completion_identity = context.completion_identity if context is not None else None
        completion_policy = completion_policy_for(receipt, completion_identity)
        try:
            self.check_environment()
            current, pane_id, workspace_id, execution_workspace = self._blocked_response_context(
                name,
                harness,
                expected_pane_id=expected_pane_id,
                context=context,
            )
            output_before = snapshot_output_receipt(
                receipt,
                completion_identity,
                lambda: self._read_receipt_output(name),
            )
            file_before = snapshot_file_receipt(receipt, execution_workspace)
            baseline_sequence = _state_change_sequence(current)
            reporter = attempt_runtime.AttemptReporter(
                context,
                name,
                pane_id,
                workspace_id,
                str(execution_workspace),
                True,
            )
            reporter.runtime_acquired(current, baseline_sequence)
            state, settlement_sequence = attempt_runtime.submit_blocked_response(
                self,
                name,
                pane_id,
                response,
                baseline_sequence,
                timeout_seconds,
                on_acceptance=lambda agent: reporter.prompt_accepted(
                    agent,
                    baseline_sequence=baseline_sequence,
                    accepted_sequence=_state_change_sequence(agent),
                ),
            )
            reporter.settled(state, sequence=settlement_sequence)
            self._raise_for_runtime_error(name, harness, state)
            if state is AgentState.BLOCKED:
                raise TransportError("agent_blocked")
            if (
                completion_policy is not CompletionPolicy.LEGACY_UNVERIFIED
                and state not in SETTLED_STATES
            ):
                completion = failed_completion(completion_policy, "agent_not_settled")
            else:
                completion = verify_completion(
                    receipt,
                    completion_identity,
                    execution_workspace,
                    prompt=response,
                    output_before=output_before,
                    file_before=file_before,
                    read_output=lambda: self._read_receipt_output(name),
                )
            reporter.receipt_observed(
                state,
                completion.task_verified,
                completion=completion,
            )
        except TransportError as exc:
            completion = failed_completion(completion_policy, exc.code)
            return DispatchOutcome(
                agent_name=name,
                state=AgentState.BLOCKED if exc.code == "agent_blocked" else AgentState.UNKNOWN,
                member_reused=True,
                pane_id=pane_id,
                error_code=exc.code,
                placement=context.placement if context is not None else None,
                execution_path=(
                    str(self._layout.execution_workspace(context)) if context is not None else None
                ),
                herdr_workspace_id=workspace_id,
                task_verified=completion.task_verified,
                error_summary=exc.summary,
                agent_settled=exc.agent_settled,
                completion=completion,
            )
        return DispatchOutcome(
            name,
            state,
            True,
            pane_id,
            placement=context.placement if context is not None else None,
            execution_path=str(execution_workspace),
            herdr_workspace_id=workspace_id,
            error_code=completion.error_code,
            task_verified=completion.task_verified,
            agent_settled=state in SETTLED_STATES,
            completion=completion,
        )

    def _blocked_response_context(
        self,
        name: str,
        harness: Harness,
        *,
        expected_pane_id: str | None,
        context: DispatchContext | None,
    ) -> tuple[Mapping[str, Any], str, str | None, Path]:
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
        workspace_id = current.get("workspace_id")
        if not isinstance(workspace_id, str):
            workspace_id = None
        execution_workspace = self._layout.execution_workspace(
            context
            or DispatchContext(
                PlacementTarget.TAB,
                harness.value,
                name,
            )
        )
        if expected_pane_id is not None:
            _validate_blocked_agent(
                current,
                name=name,
                pane_id=pane_id,
                expected_pane_id=expected_pane_id,
                execution_workspace=execution_workspace,
            )
        return current, pane_id, workspace_id, execution_workspace

    def close_created_agent(self, name: str) -> None:
        terminal = self._created_terminals.pop(name, None)
        if terminal is None:
            return
        self._layout.close_temporary(terminal)

    def close_agent_terminal(
        self,
        name: str,
        placement: PlacementTarget,
        *,
        expected_pane_id: str,
        dry_run: bool,
    ) -> dict[str, object]:
        if placement is PlacementTarget.WORKTREE:
            raise TransportError("worktree_cleanup_forbidden")
        self.check_environment()
        try:
            agent = _agent_payload(
                run_json(
                    self.runner,
                    Command(
                        ["herdr", "agent", "get", name],
                        self.workspace,
                        CONTROL_TIMEOUT_SECONDS,
                    ),
                )
            )
        except TransportError as exc:
            if exc.code == "agent_not_found":
                return {
                    "agent_name": name,
                    "placement": placement.value,
                    "action": "already_absent",
                }
            raise
        if agent.get("name") != name:
            raise TransportError("agent_identity_mismatch")
        if _non_empty_string(agent, "pane_id") != expected_pane_id:
            raise TransportError("agent_pane_mismatch")
        if agent.get("workspace_id") != self.environ.get("HERDR_WORKSPACE_ID"):
            raise TransportError("agent_workspace_mismatch")
        for key in ("cwd", "foreground_cwd"):
            value = agent.get(key)
            if not isinstance(value, str) or not Path(value).resolve().is_relative_to(
                self.workspace
            ):
                raise TransportError("agent_workspace_mismatch")
        if _agent_state(agent) not in SETTLED_STATES:
            raise TransportError("agent_cleanup_not_settled")
        target_kind = "pane"
        target_id = expected_pane_id
        if not dry_run:
            run_json(
                self.runner,
                Command(
                    ["herdr", target_kind, "close", target_id],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
        return {
            "agent_name": name,
            "placement": placement.value,
            "target": target_id,
            "action": "would_close" if dry_run else "closed",
        }

    def _bounded_runner(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        deadline = getattr(self._dispatch_deadline, "value", None)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError("herdr_timeout")
            timeout = remaining if timeout is None else min(timeout, remaining)
        return self._raw_runner(argv, cwd=cwd, timeout=timeout)

    def _remaining_seconds(self, maximum: float) -> float:
        deadline = getattr(self._dispatch_deadline, "value", None)
        if not isinstance(deadline, (int, float)):
            return maximum
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            raise TransportError("herdr_timeout")
        return min(maximum, remaining)

    def _remaining_milliseconds(self, maximum: int) -> int:
        return max(1, min(maximum, int(self._remaining_seconds(maximum / 1000) * 1000)))

    def _sleep_until(self, seconds: float, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError("herdr_timeout")
        self.sleeper(min(seconds, remaining))
        if seconds >= remaining:
            raise TransportError("herdr_timeout")

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
            expected_workspace_id = (
                self._layout.workspace_id
                if context.placement is not PlacementTarget.WORKTREE
                else None
            )
            _validate_reusable_agent(
                agent,
                name,
                harness,
                execution_workspace,
                expected_workspace_id=expected_workspace_id,
            )
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
            started = self._start_agent(
                name,
                harness,
                pane_id,
                execution_workspace,
            )
            started_agent = _agent_payload(started)
            started_state = _agent_state(started_agent)
            if started_state not in SETTLED_STATES | {AgentState.BLOCKED}:
                recovery_timeout_ms = self._remaining_milliseconds(START_RECOVERY_TIMEOUT_MS)
                started = run_json(
                    self.runner,
                    Command(
                        [
                            "herdr",
                            "agent",
                            "wait",
                            name,
                            "--timeout",
                            str(recovery_timeout_ms),
                        ],
                        self.workspace,
                        recovery_timeout_ms / 1000,
                    ),
                )
            self._sleep_until(
                3,
                getattr(self._dispatch_deadline, "value", float("inf")),
            )
            started_agent = self._wait_for_interactive_agent(name, harness, pane_id)
            _validate_started_agent(started_agent, name, harness, pane_id)
        except TransportError as cause:
            if cause.code in {"agent_blocked", "herdr_timeout"}:
                self._created_terminals[name] = terminal
                raise
            try:
                self._layout.cleanup_failed(terminal)
            except TransportError:
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
        deadline = min(
            time.monotonic() + AGENT_INTERACTIVE_READY_TIMEOUT_SECONDS,
            getattr(self._dispatch_deadline, "value", float("inf")),
        )
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
            self._sleep_until(0.5, deadline)

    def _wait_for_shell(self, pane_id: str) -> None:
        deadline = min(
            time.monotonic() + SHELL_READY_TIMEOUT_SECONDS,
            getattr(self._dispatch_deadline, "value", float("inf")),
        )
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
            self._sleep_until(0.25, deadline)

    def _start_agent(
        self,
        name: str,
        harness: Harness,
        pane_id: str,
        execution_workspace: Path,
    ) -> Mapping[str, Any]:
        for attempt in range(3):
            start_timeout_ms = self._remaining_milliseconds(START_TIMEOUT_MS)
            native_arguments = MAXIMUM_AUTOMATION_ARGUMENTS[harness]
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
                    str(start_timeout_ms),
                    "--",
                    *native_arguments,
                ],
                self.workspace,
                start_timeout_ms / 1000,
            )
            try:
                started = run_json(self.runner, command)
            except TransportError as exc:
                if exc.code == "agent_pane_busy" and attempt < 2:
                    self._sleep_until(
                        1,
                        getattr(self._dispatch_deadline, "value", float("inf")),
                    )
                    continue
                if exc.code == "agent_not_ready":
                    self._accept_claude_workspace_trust(
                        name,
                        harness,
                        execution_workspace,
                    )
                    recovery_timeout_ms = self._remaining_milliseconds(START_RECOVERY_TIMEOUT_MS)
                    return run_json(
                        self.runner,
                        Command(
                            [
                                "herdr",
                                "agent",
                                "wait",
                                name,
                                "--timeout",
                                str(recovery_timeout_ms),
                            ],
                            self.workspace,
                            recovery_timeout_ms / 1000,
                        ),
                    )
                raise
            started_agent = _agent_payload(started)
            startup_blocked = started_agent.get("agent_status") == AgentState.BLOCKED.value
            if startup_blocked and self._accept_claude_workspace_trust(
                name,
                harness,
                execution_workspace,
            ):
                recovery_timeout_ms = self._remaining_milliseconds(START_RECOVERY_TIMEOUT_MS)
                return run_json(
                    self.runner,
                    Command(
                        [
                            "herdr",
                            "agent",
                            "wait",
                            name,
                            "--timeout",
                            str(recovery_timeout_ms),
                        ],
                        self.workspace,
                        recovery_timeout_ms / 1000,
                    ),
                )
            return started
        raise TransportError("agent_start_failed")

    def _accept_claude_workspace_trust(
        self,
        name: str,
        harness: Harness,
        execution_workspace: Path,
    ) -> bool:
        if harness is not Harness.CLAUDE:
            return False
        try:
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
                        "160",
                    ],
                    self.workspace,
                    CONTROL_TIMEOUT_SECONDS,
                ),
            )
        except TransportError:
            return False
        expected_path = str(execution_workspace.resolve())
        if (
            not all(marker in output for marker in CLAUDE_WORKSPACE_TRUST_MARKERS)
            or re.search(
                rf"(?m)^\s*{re.escape(expected_path)}\s*$",
                output,
            )
            is None
        ):
            return False
        run_json(
            self.runner,
            Command(
                ["herdr", "agent", "send-keys", name, "enter"],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )
        return True

    def _prompt(
        self,
        name: str,
        harness: Harness,
        prompt: str,
        timeout_seconds: float,
        *,
        reporter: attempt_runtime.AttemptReporter,
    ) -> Mapping[str, Any]:
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
        reporter.runtime_acquired(before, baseline_sequence)
        acceptance_reported = False

        def report_acceptance(agent: Mapping[str, Any]) -> None:
            nonlocal acceptance_reported
            sequence = _state_change_sequence(agent)
            if acceptance_reported or sequence <= baseline_sequence:
                return
            acceptance_reported = True
            reporter.prompt_accepted(
                agent,
                baseline_sequence=baseline_sequence,
                accepted_sequence=sequence,
            )

        deadline = getattr(
            self._dispatch_deadline,
            "value",
            time.monotonic() + timeout_seconds,
        )
        prompt_command_timeout = self._remaining_seconds(timeout_seconds)
        prompt_wait_timeout_ms = max(
            1,
            int(
                max(
                    0.001,
                    prompt_command_timeout - PROMPT_COMMAND_GRACE_SECONDS,
                )
                * 1000
            ),
        )
        acceptance_started = time.monotonic()
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
                            str(prompt_wait_timeout_ms),
                        ],
                        self.workspace,
                        prompt_command_timeout,
                    ),
                )
            )
        except TransportError as exc:
            if exc.code not in {"agent_prompt_stalled", *PROMPT_TIMEOUT_ERRORS}:
                raise
            try:
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
            except TransportError as reconcile_error:
                if exc.code not in PROMPT_TIMEOUT_ERRORS:
                    raise
                raise TransportError(
                    "prompt_acceptance_timeout",
                    summary=attempt_runtime.prompt_acceptance_summary(
                        acceptance_started,
                        baseline_sequence,
                        None,
                    ),
                ) from reconcile_error
            state = _agent_state(stalled)
            sequence = _state_change_sequence(stalled)
            if exc.code in PROMPT_TIMEOUT_ERRORS and sequence <= baseline_sequence:
                raise TransportError(
                    "prompt_acceptance_timeout",
                    summary=attempt_runtime.prompt_acceptance_summary(
                        acceptance_started,
                        sequence,
                        state,
                    ),
                ) from exc
            if sequence == baseline_sequence and state is AgentState.IDLE:
                stalled = self._resubmit_enter_until_turn(name, baseline_sequence, deadline)
                state = _agent_state(stalled)
                sequence = _state_change_sequence(stalled)
            if sequence <= baseline_sequence:
                raise TransportError("agent_turn_not_observed") from exc
            report_acceptance(stalled)
            if state in {
                AgentState.IDLE,
                AgentState.DONE,
                AgentState.BLOCKED,
            }:
                return self._confirm_stable_settlement(name, stalled, deadline)
            if state is not AgentState.WORKING:
                raise TransportError("agent_not_settled") from exc
        else:
            state = _agent_state(prompted)
            sequence = _state_change_sequence(prompted)
            if sequence <= baseline_sequence:
                raise TransportError("agent_turn_not_observed")
            report_acceptance(prompted)
            if state in {
                AgentState.IDLE,
                AgentState.DONE,
                AgentState.BLOCKED,
            }:
                return self._confirm_stable_settlement(name, prompted, deadline)
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
                return self._confirm_stable_settlement(name, current, deadline)
            self._sleep_until(0.5, deadline)

    def _resubmit_enter_until_turn(
        self,
        name: str,
        baseline_sequence: int,
        deadline: float,
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
            self._sleep_until(0.5, deadline)
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
        current: Mapping[str, Any],
        deadline: float,
    ) -> Mapping[str, Any]:
        state = _agent_state(current)
        if state is AgentState.BLOCKED:
            return current

        confirmations = 0
        while confirmations < self.settled_confirmation_polls:
            if time.monotonic() >= deadline:
                raise TransportError("herdr_timeout")
            self._sleep_until(0.5, deadline)
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
                return current
            if state in SETTLED_STATES:
                confirmations += 1
            else:
                confirmations = 0
        return current

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
        if INVALID_MODEL_FAILURE.search(output):
            raise TransportError(
                "agent_model_invalid",
                summary=_matched_error_summary(output, INVALID_MODEL_FAILURE),
                agent_settled=True,
            )
        if PROVIDER_RETRY_FAILURE.search(output):
            raise TransportError(
                "agent_provider_failed",
                summary=_matched_error_summary(output, PROVIDER_RETRY_FAILURE),
                agent_settled=True,
            )
        if AUTH_FAILURE.search(output) or (
            harness is Harness.CLAUDE and CLAUDE_AUTH_FAILURE.search(output)
        ):
            pattern = AUTH_FAILURE if AUTH_FAILURE.search(output) else CLAUDE_AUTH_FAILURE
            raise TransportError(
                "agent_auth_failed",
                summary=_matched_error_summary(output, pattern),
                agent_settled=True,
            )
        if AUTH_REQUIRED_FAILURE.search(output):
            raise TransportError(
                "agent_auth_required",
                summary=_matched_error_summary(output, AUTH_REQUIRED_FAILURE),
                agent_settled=True,
            )

    def _verify_task_receipt(
        self,
        name: str,
        receipt: TaskReceipt | None,
        execution_workspace: Path,
        *,
        prompt: str,
        output_before: str | None,
        file_before: FileReceiptSnapshot | None,
    ) -> bool | None:
        return verify_legacy_receipt(
            receipt,
            execution_workspace,
            prompt=prompt,
            output_before=output_before,
            file_before=file_before,
            read_output=lambda: self._read_receipt_output(name),
        )

    _receipt_file_path = staticmethod(receipt_file_path)
    _file_receipt_snapshot = staticmethod(file_receipt_snapshot)

    def _read_receipt_output(self, name: str) -> str:
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
                    "120",
                ],
                self.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )


def _matched_error_summary(output: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(output)
    if match is None:
        return ""
    return re.sub(r"\s+", " ", match.group(0)).strip()[:300]


def stable_agent_name(workflow_name: str, workspace: Path, harness: Harness) -> str:
    seed = f"{workflow_name}\0{workspace.resolve()}\0{harness.value}"
    digest = hashlib.sha256(seed.encode()).hexdigest()[:8]
    return f"ho-{harness.value}-{digest}"


def doctor_agent_name(workflow_name: str, harness: Harness) -> str:
    digest = hashlib.sha256(f"{workflow_name}\0doctor\0{harness.value}".encode()).hexdigest()[:6]
    return f"doctor-{harness.value}-{digest}"


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
        seed = f"{workflow_name}\0{workspace.resolve()}\0{harness.value}" f"\0{placement.value}"
        digest = hashlib.sha256(seed.encode()).hexdigest()[:6]
        return (f"ho-{harness.value}{target}-{digest}"[:32],)
    seed = f"{workflow_name}\0{workspace.resolve()}\0{harness.value}" f"\0{placement.value}"
    digest = hashlib.sha256(seed.encode()).hexdigest()[:6]
    return tuple(
        f"ho-{harness.value}{target}-{index:02d}-{digest}"[:32] for index in range(1, replicas + 1)
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
    *,
    expected_workspace_id: str | None = None,
) -> None:
    live_name = agent.get("name")
    if live_name is not None and live_name != name:
        raise TransportError("agent_identity_mismatch")
    if agent.get("agent") != harness.value:
        raise TransportError("agent_identity_mismatch")
    reported_workspace_id = agent.get("workspace_id")
    if (
        expected_workspace_id
        and reported_workspace_id is not None
        and (
            not isinstance(reported_workspace_id, str)
            or reported_workspace_id.strip() != expected_workspace_id
        )
    ):
        raise TransportError("agent_workspace_mismatch")
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


def _validate_blocked_agent(
    current: Mapping[str, Any],
    *,
    name: str,
    pane_id: str,
    expected_pane_id: str,
    execution_workspace: Path,
) -> None:
    if current.get("name") != name:
        raise TransportError("agent_identity_mismatch")
    if pane_id != expected_pane_id:
        raise TransportError("agent_pane_mismatch")
    for key in ("cwd", "foreground_cwd"):
        value = current.get(key)
        if not isinstance(value, str) or not Path(value).resolve().is_relative_to(
            execution_workspace.resolve()
        ):
            raise TransportError("agent_workspace_mismatch")
