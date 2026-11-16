"""Pure identity and outcome helpers for coordinator dispatch."""

from __future__ import annotations

import hashlib
from pathlib import Path

from herdr_orchestrator.completion import CompletionIdentity, CompletionPolicy
from herdr_orchestrator.model import AgentState, ClaimedJob, DispatchOutcome, Harness, JobState


def _completion_identity(job: ClaimedJob) -> CompletionIdentity | None:
    if job.completion_policy is not CompletionPolicy.STRUCTURED_V2:
        return None
    return CompletionIdentity(job.job_id, job.attempt, job.fencing_token)


def _failure_outcome(
    job: ClaimedJob,
    error_code: str,
    *,
    member_reused: bool = False,
    pane_id: str | None = None,
    error_summary: str | None = None,
    agent_settled: bool | None = None,
) -> DispatchOutcome:
    return DispatchOutcome(
        agent_name=job.agent_name,
        state=(AgentState.BLOCKED if error_code == "agent_blocked" else AgentState.UNKNOWN),
        member_reused=member_reused,
        pane_id=pane_id,
        error_code=error_code,
        placement=job.placement,
        error_summary=error_summary,
        agent_settled=agent_settled,
        correlation_id=job.correlation_id,
    )


def _controller_agent_name(
    workflow_name: str,
    workspace: Path,
    harness: Harness,
) -> str:
    digest = hashlib.sha256(
        f"{workflow_name}\0{workspace.resolve()}\0controller\0{harness.value}".encode()
    ).hexdigest()[:8]
    return f"ho-control-{harness.value}-{digest}"


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("integer_value_invalid")
    return int(value)


def _controller_turn_failed(outcome: DispatchOutcome) -> str | None:
    if outcome.error_code is not None:
        return outcome.error_code
    if outcome.state not in {AgentState.IDLE, AgentState.DONE}:
        return outcome.state.value
    return None


def _queue_is_idle(counts: dict[str, int]) -> bool:
    return all(
        counts[state.value] == 0 for state in (JobState.PENDING, JobState.RUNNING, JobState.BLOCKED)
    )


def _gc_target_values(target_states: set[JobState]) -> set[str]:
    if not target_states or not target_states <= {
        JobState.SUCCEEDED,
        JobState.FAILED,
    }:
        raise ValueError("gc_states_invalid")
    return {state.value for state in target_states}
