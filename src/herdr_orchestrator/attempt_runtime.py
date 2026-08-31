from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    AttemptRuntime,
    DispatchContext,
    DispatchOutcome,
    Harness,
)
from herdr_orchestrator.protocol import Command, CommandRunner, TransportError, run_json

CONTROL_TIMEOUT_SECONDS = 10
AMBIGUOUS_RESPONSE_SUBMISSION_ERRORS = frozenset(
    {
        "herdr_command_failed",
        "herdr_invalid_response",
        "herdr_timeout",
        "herdr_unavailable",
        "timeout",
    }
)
OUTPUT_RECEIPT_UI_MARKERS = {"\u26ec", "⧬", "⏺", "•", "●", "◆", "◇", "✦"}


class RecoveryHost(Protocol):
    workspace: Path
    runner: CommandRunner
    sleeper: Callable[[float], None]
    settled_confirmation_polls: int
    _dispatch_deadline: Any

    def check_environment(self) -> None: ...

    def _sleep_until(self, seconds: float, deadline: float) -> None: ...

    def _confirm_stable_settlement(
        self,
        name: str,
        current: Mapping[str, Any],
        deadline: float,
    ) -> Mapping[str, Any]: ...

    def _raise_for_runtime_error(
        self,
        name: str,
        harness: Harness,
        state: AgentState,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _RecoveryObservation:
    state: AgentState
    sequence: int


def elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def line_starts_with_receipt(line: str, receipt: str) -> bool:
    normalized = line.strip()
    if normalized.startswith(receipt):
        return True
    return bool(
        normalized
        and normalized[0] in OUTPUT_RECEIPT_UI_MARKERS
        and normalized[1:].lstrip().startswith(receipt)
    )


def prompt_acceptance_summary(
    started: float,
    state_change_sequence: int,
    state: AgentState | None,
) -> str:
    elapsed = elapsed_ms(started)
    state_value = state.value if state is not None else "unobserved"
    return (
        f"phase=prompt_acceptance elapsed_ms={elapsed} "
        f"state={state_value} state_change_seq={state_change_sequence}"
    )


def agent_session_identity(agent: Mapping[str, Any]) -> str | None:
    session = agent.get("agent_session")
    if not isinstance(session, Mapping):
        return None
    value = session.get("value")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


@dataclass(frozen=True, slots=True)
class AttemptReporter:
    context: DispatchContext | None
    agent_name: str
    pane_id: str | None
    workspace_id: str | None
    execution_path: str
    member_reused: bool

    def runtime_acquired(self, agent: Mapping[str, Any], sequence: int) -> None:
        self._emit(
            AttemptProgress(
                AttemptPhase.RUNTIME_ACQUIRED,
                self.agent_name,
                pane_id=self.pane_id,
                herdr_workspace_id=self.workspace_id,
                execution_path=self.execution_path,
                agent_session_id=agent_session_identity(agent),
                prompt_baseline_sequence=sequence,
                state_change_sequence=sequence,
                agent_state=_state(agent),
                member_reused=self.member_reused,
            )
        )

    def prompt_accepted(
        self,
        agent: Mapping[str, Any],
        *,
        baseline_sequence: int,
        accepted_sequence: int,
    ) -> None:
        self._emit(
            AttemptProgress(
                AttemptPhase.PROMPT_ACCEPTED,
                self.agent_name,
                pane_id=self.pane_id,
                herdr_workspace_id=self.workspace_id,
                execution_path=self.execution_path,
                agent_session_id=agent_session_identity(agent),
                prompt_baseline_sequence=baseline_sequence,
                prompt_accepted_sequence=accepted_sequence,
                state_change_sequence=accepted_sequence,
                agent_state=_state(agent),
                member_reused=self.member_reused,
            )
        )

    def settled(self, state: AgentState, *, sequence: int | None = None) -> None:
        self._emit(
            AttemptProgress(
                AttemptPhase.SETTLED,
                self.agent_name,
                pane_id=self.pane_id,
                herdr_workspace_id=self.workspace_id,
                execution_path=self.execution_path,
                state_change_sequence=sequence,
                agent_state=state,
                member_reused=self.member_reused,
                agent_settled=state in {AgentState.IDLE, AgentState.DONE},
            )
        )

    def receipt_observed(
        self,
        state: AgentState,
        task_verified: bool | None,
        *,
        sequence: int | None = None,
    ) -> None:
        self._emit(
            AttemptProgress(
                AttemptPhase.RECEIPT_OBSERVED,
                self.agent_name,
                pane_id=self.pane_id,
                herdr_workspace_id=self.workspace_id,
                execution_path=self.execution_path,
                state_change_sequence=sequence,
                agent_state=state,
                member_reused=self.member_reused,
                agent_settled=state in {AgentState.IDLE, AgentState.DONE},
                task_verified=task_verified,
            )
        )

    def _emit(self, progress: AttemptProgress) -> None:
        if self.context is not None and self.context.attempt_progress is not None:
            self.context.attempt_progress(progress)


def _state(agent: Mapping[str, Any]) -> AgentState:
    value = agent.get("agent_status")
    if not isinstance(value, str):
        return AgentState.UNKNOWN
    try:
        return AgentState(value)
    except ValueError:
        return AgentState.UNKNOWN


def recover_turn(
    host: RecoveryHost,
    harness: Harness,
    *,
    timeout_seconds: float,
    agent_name: str,
    context: DispatchContext,
    runtime: AttemptRuntime,
) -> DispatchOutcome:
    reporter = AttemptReporter(
        context,
        agent_name,
        runtime.pane_id,
        runtime.herdr_workspace_id,
        runtime.execution_path or str(host.workspace),
        True,
    )
    pane_id = runtime.pane_id
    try:
        host.check_environment()
        deadline = time.monotonic() + timeout_seconds
        observation = _initial_recovery_observation(host, harness, runtime)
        if observation is None:
            return _recovery_outcome(
                context,
                runtime,
                state=AgentState.UNKNOWN,
                error_code="lease_expired_unaccepted",
                agent_settled=False,
            )
        state, sequence = _confirm_recovered_settlement(
            host,
            harness,
            runtime,
            state=observation.state,
            sequence=observation.sequence,
            deadline=deadline,
        )
        if state not in {AgentState.IDLE, AgentState.DONE, AgentState.BLOCKED}:
            raise TransportError("unsafe_turn_adoption")
        try:
            host._raise_for_runtime_error(agent_name, harness, state)
        except TransportError as exc:
            if not exc.agent_settled:
                raise
            return _recovery_outcome(
                context,
                runtime,
                state=state,
                error_code=exc.code,
                error_summary=exc.summary,
                agent_settled=True,
            )
        verified = (
            None
            if context.receipt is None
            else runtime.task_verified if runtime.phase is AttemptPhase.RECEIPT_OBSERVED else False
        )
        if runtime.phase is not AttemptPhase.RECEIPT_OBSERVED:
            reporter.receipt_observed(state, verified, sequence=sequence)
        return DispatchOutcome(
            agent_name,
            state,
            True,
            pane_id,
            error_code=(
                "agent_blocked"
                if state is AgentState.BLOCKED
                else (
                    "task_receipt_recovery_unverified"
                    if context.receipt is not None and verified is not True
                    else None
                )
            ),
            placement=context.placement,
            execution_path=runtime.execution_path,
            herdr_workspace_id=runtime.herdr_workspace_id,
            task_verified=verified,
            agent_settled=(
                runtime.agent_settled
                if runtime.agent_settled is not None
                else state in {AgentState.IDLE, AgentState.DONE}
            ),
        )
    except TransportError as exc:
        return _recovery_outcome(
            context,
            runtime,
            state=AgentState.UNKNOWN,
            error_code="unsafe_turn_adoption",
            error_summary=exc.summary,
            agent_settled=False,
        )


def _initial_recovery_observation(
    host: RecoveryHost,
    harness: Harness,
    runtime: AttemptRuntime,
) -> _RecoveryObservation | None:
    if (
        runtime.phase is AttemptPhase.CLAIMED
        and runtime.pane_id is None
        and runtime.execution_path is None
        and runtime.prompt_baseline_sequence is None
    ):
        return None
    current = _read_agent(host, runtime.agent_name)
    _validate_runtime(current, harness, runtime)
    state = _state(current)
    baseline = runtime.prompt_baseline_sequence
    sequence = _sequence(current)
    if runtime.phase in {AttemptPhase.CLAIMED, AttemptPhase.RUNTIME_ACQUIRED}:
        if baseline is None and runtime.phase is AttemptPhase.CLAIMED:
            return None
        if (
            baseline is not None
            and runtime.prompt_accepted_sequence is None
            and sequence == baseline
            and state
            in {
                AgentState.IDLE,
                AgentState.DONE,
                AgentState.BLOCKED,
            }
        ):
            return None
        raise TransportError("unsafe_turn_adoption")
    if runtime.phase not in {AttemptPhase.SETTLED, AttemptPhase.RECEIPT_OBSERVED}:
        raise TransportError("unsafe_turn_adoption")
    expected_sequence = runtime.state_change_sequence
    if (
        expected_sequence is None
        or sequence != expected_sequence
        or state
        not in {
            AgentState.IDLE,
            AgentState.DONE,
            AgentState.BLOCKED,
        }
        or (runtime.agent_state is not None and state is not runtime.agent_state)
    ):
        raise TransportError("unsafe_turn_adoption")
    return _RecoveryObservation(state, sequence)


def _confirm_recovered_settlement(
    host: RecoveryHost,
    harness: Harness,
    runtime: AttemptRuntime,
    *,
    state: AgentState,
    sequence: int,
    deadline: float,
) -> tuple[AgentState, int]:
    if state is AgentState.BLOCKED:
        return state, sequence
    for _ in range(host.settled_confirmation_polls):
        if time.monotonic() >= deadline:
            raise TransportError("unsafe_turn_adoption")
        host._sleep_until(0.5, deadline)
        current = _read_agent(host, runtime.agent_name)
        _validate_runtime(current, harness, runtime)
        current_sequence = _sequence(current)
        current_state = _state(current)
        if current_sequence != sequence or current_state is not state:
            raise TransportError("unsafe_turn_adoption")
    return state, sequence


def submit_blocked_response(
    host: RecoveryHost,
    name: str,
    pane_id: str,
    response: str,
    baseline_sequence: int,
    timeout_seconds: int,
    *,
    on_acceptance: Callable[[Mapping[str, Any]], None] | None = None,
) -> tuple[AgentState, int]:
    accepted = False

    def observe_acceptance(agent: Mapping[str, Any]) -> None:
        nonlocal accepted
        accepted = True
        if on_acceptance is not None:
            on_acceptance(agent)

    def reconcile() -> tuple[AgentState, int]:
        return wait_after_response(
            host,
            name,
            baseline_sequence,
            timeout_seconds,
            on_acceptance=observe_acceptance,
        )

    def reconcile_submission(
        submission_error: TransportError | None = None,
    ) -> tuple[AgentState, int]:
        try:
            return reconcile()
        except TransportError as reconciliation_error:
            if accepted:
                raise
            raise TransportError(
                "unsafe_turn_adoption",
                summary=(
                    reconciliation_error.summary
                    or (submission_error.summary if submission_error is not None else None)
                ),
            ) from reconciliation_error

    try:
        run_json(
            host.runner,
            Command(
                ["herdr", "pane", "run", pane_id, response],
                host.workspace,
                CONTROL_TIMEOUT_SECONDS,
            ),
        )
    except TransportError as submission_error:
        if submission_error.code not in AMBIGUOUS_RESPONSE_SUBMISSION_ERRORS:
            raise
        return reconcile_submission(submission_error)
    return reconcile_submission()


def wait_after_response(
    host: RecoveryHost,
    name: str,
    baseline_sequence: int,
    timeout_seconds: int,
    *,
    on_acceptance: Callable[[Mapping[str, Any]], None] | None = None,
) -> tuple[AgentState, int]:
    deadline = min(
        time.monotonic() + timeout_seconds,
        getattr(host._dispatch_deadline, "value", float("inf")),
    )
    while True:
        current = _read_agent(host, name)
        state = _state(current)
        sequence = _sequence(current)
        if sequence > baseline_sequence and on_acceptance is not None:
            on_acceptance(current)
            on_acceptance = None
        if sequence > baseline_sequence and state in {
            AgentState.IDLE,
            AgentState.DONE,
            AgentState.BLOCKED,
        }:
            current = host._confirm_stable_settlement(name, current, deadline)
            return _state(current), _sequence(current)
        if time.monotonic() >= deadline:
            raise TransportError("herdr_timeout")
        host.sleeper(0.5)


def _read_agent(host: RecoveryHost, name: str) -> Mapping[str, Any]:
    result = run_json(
        host.runner,
        Command(["herdr", "agent", "get", name], host.workspace, CONTROL_TIMEOUT_SECONDS),
    )
    agent = result.get("agent")
    if not isinstance(agent, Mapping):
        raise TransportError("unsafe_turn_adoption")
    return agent


def _validate_runtime(
    agent: Mapping[str, Any],
    harness: Harness,
    runtime: AttemptRuntime,
) -> None:
    if runtime.pane_id is None or runtime.execution_path is None:
        raise TransportError("unsafe_turn_adoption")
    live_name = agent.get("name")
    if live_name is not None and live_name != runtime.agent_name:
        raise TransportError("unsafe_turn_adoption")
    if agent.get("agent") != harness.value or agent.get("pane_id") != runtime.pane_id:
        raise TransportError("unsafe_turn_adoption")
    if (
        runtime.herdr_workspace_id is not None
        and agent.get("workspace_id") != runtime.herdr_workspace_id
    ):
        raise TransportError("unsafe_turn_adoption")
    expected_root = Path(runtime.execution_path).resolve()
    for key in ("cwd", "foreground_cwd"):
        value = agent.get(key)
        if not isinstance(value, str) or Path(value).resolve() != expected_root:
            raise TransportError("unsafe_turn_adoption")
    if (
        runtime.agent_session_id is not None
        and agent_session_identity(agent) != runtime.agent_session_id
    ):
        raise TransportError("unsafe_turn_adoption")


def _sequence(agent: Mapping[str, Any]) -> int:
    value = agent.get("state_change_seq")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TransportError("unsafe_turn_adoption")
    return value


def _recovery_outcome(
    context: DispatchContext,
    runtime: AttemptRuntime,
    *,
    state: AgentState,
    error_code: str,
    error_summary: str | None = None,
    agent_settled: bool | None = None,
) -> DispatchOutcome:
    return DispatchOutcome(
        runtime.agent_name,
        state,
        True,
        runtime.pane_id,
        error_code=error_code,
        placement=context.placement,
        execution_path=runtime.execution_path,
        herdr_workspace_id=runtime.herdr_workspace_id,
        task_verified=runtime.task_verified,
        error_summary=error_summary,
        agent_settled=agent_settled,
    )
