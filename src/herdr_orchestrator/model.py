from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from herdr_orchestrator.completion import (
    CompletionIdentity,
    CompletionPolicy,
    CompletionResult,
)
from herdr_orchestrator.completion import (
    ReceiptKind as ReceiptKind,
)
from herdr_orchestrator.completion import (
    TaskReceipt as TaskReceipt,
)


class Harness(StrEnum):
    DROID = "droid"
    GROK = "grok"
    CODEX = "codex"
    PI = "pi"
    CLAUDE = "claude"
    HERMES = "hermes"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"


class AgentState(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"


class AttemptPhase(StrEnum):
    CLAIMED = "claimed"
    RUNTIME_ACQUIRED = "runtime_acquired"
    PROMPT_ACCEPTED = "prompt_accepted"
    SETTLED = "settled"
    RECEIPT_OBSERVED = "receipt_observed"
    OUTCOME_COMMITTED = "outcome_committed"
    ABANDONED = "abandoned"
    ATTENTION = "attention"


@dataclass(frozen=True, slots=True)
class AttemptRuntime:
    agent_name: str
    pane_id: str | None
    herdr_workspace_id: str | None
    execution_path: str | None
    agent_session_id: str | None
    prompt_baseline_sequence: int | None
    prompt_accepted_sequence: int | None
    state_change_sequence: int | None
    phase: AttemptPhase = AttemptPhase.CLAIMED
    agent_state: AgentState | None = None
    agent_settled: bool | None = None
    task_verified: bool | None = None
    completion: CompletionResult | None = None


@dataclass(frozen=True, slots=True)
class AttemptTransition:
    job_id: int
    attempt_id: int
    attempt: int
    operation_sequence: int
    phase: AttemptPhase


class PlacementMode(StrEnum):
    HYBRID = "hybrid"
    TAB = "tab"
    PANE = "pane"
    WORKTREE = "worktree"


class PlacementTarget(StrEnum):
    TAB = "tab"
    PANE = "pane"
    WORKTREE = "worktree"


class TrackerBackend(StrEnum):
    LOCAL_MARKDOWN = "local-markdown"
    GITHUB = "github"


class WayfinderMode(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class CoordinatorConfig:
    poll_seconds: int
    max_parallel: int
    lease_seconds: int
    max_attempts: int
    agent_timeout_seconds: int
    readiness_ttl_seconds: int = 3600
    readiness_cooldown_seconds: int = 300
    readiness_probe_timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class HarnessHealthConfig:
    """Bounded persistence policy for readiness evidence."""

    ttl_seconds: int = 3600
    cooldown_seconds: int = 300
    probe_timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class PlacementConfig:
    mode: PlacementMode
    worktree_root: Path


@dataclass(frozen=True, slots=True)
class StandardizedDeliveryConfig:
    tracker_backend: TrackerBackend
    tracker_root: Path
    artifact_root: Path
    github_repository: str | None
    wayfinder: WayfinderMode
    max_parallel: int
    review_repair_rounds: int


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    enabled: bool
    harness: Harness | None
    worker_harnesses: tuple[Harness, ...]
    interval_seconds: int
    prompt_file: Path
    output_file: Path
    max_tasks: int


@dataclass(frozen=True, slots=True)
class HarnessProfile:
    schema_version: int
    harness: Harness
    display_name: str
    summary: str
    strengths: tuple[str, ...]
    best_for: tuple[str, ...]
    avoid_for: tuple[str, ...]
    traits: tuple[str, ...]
    context_file: Path


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    name: str
    harness: Harness
    capabilities: tuple[str, ...]
    replicas: int = 1
    placement: PlacementTarget | None = None


@dataclass(frozen=True, slots=True)
class SeedJobConfig:
    title: str
    harness: Harness
    prompt_file: Path
    dedupe_key: str
    placement: PlacementTarget | None = None


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    schema_version: int
    name: str
    path: Path
    workspace: Path
    state_db: Path
    coordinator: CoordinatorConfig
    placement: PlacementConfig
    standardized_delivery: StandardizedDeliveryConfig
    planner: PlannerConfig
    profiles_dir: Path
    profiles: tuple[HarnessProfile, ...]
    workers: tuple[WorkerConfig, ...]
    seed_jobs: tuple[SeedJobConfig, ...]
    harness_health: HarnessHealthConfig = HarnessHealthConfig()


@dataclass(frozen=True, slots=True)
class NewJob:
    workflow: str
    title: str
    harness: Harness
    prompt: str
    dedupe_key: str
    max_attempts: int
    placement: PlacementTarget | None = PlacementTarget.TAB
    receipt: TaskReceipt | None = None
    completion_policy: CompletionPolicy | None = None
    workspace: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: int
    workflow: str
    title: str
    harness: Harness
    prompt: str
    dedupe_key: str
    attempt: int
    max_attempts: int
    agent_name: str
    placement: PlacementTarget
    receipt: TaskReceipt | None = None
    correlation_id: str = ""
    attempt_id: int = 0
    fencing_token: str = ""
    lease_owner: str = ""
    lease_until: float = 0.0
    operation_token: str = ""
    operation_sequence: int = 0
    phase: AttemptPhase = AttemptPhase.CLAIMED
    recovery: bool = False
    runtime: AttemptRuntime | None = None
    completion_policy: CompletionPolicy = CompletionPolicy.LEGACY_UNVERIFIED


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    agent_name: str
    state: AgentState
    member_reused: bool
    pane_id: str | None
    error_code: str | None = None
    placement: PlacementTarget | None = None
    execution_path: str | None = None
    herdr_workspace_id: str | None = None
    task_verified: bool | None = None
    error_summary: str | None = None
    agent_settled: bool | None = None
    phase_timings_ms: dict[str, int] | None = None
    correlation_id: str = ""
    completion: CompletionResult | None = None


@dataclass(frozen=True, slots=True)
class AttemptProgress:
    phase: AttemptPhase
    agent_name: str
    pane_id: str | None = None
    herdr_workspace_id: str | None = None
    execution_path: str | None = None
    agent_session_id: str | None = None
    prompt_baseline_sequence: int | None = None
    prompt_accepted_sequence: int | None = None
    state_change_sequence: int | None = None
    agent_state: AgentState | None = None
    member_reused: bool | None = None
    agent_settled: bool | None = None
    task_verified: bool | None = None
    error_code: str | None = None
    error_summary: str | None = None
    completion: CompletionResult | None = None


@dataclass(frozen=True, slots=True)
class DispatchContext:
    placement: PlacementTarget
    title: str
    task_key: str
    batch_key: str | None = None
    worktree_root: Path | None = None
    receipt: TaskReceipt | None = None
    correlation_id: str = ""
    attempt_progress: Callable[[AttemptProgress], None] | None = None
    completion_identity: CompletionIdentity | None = None


@dataclass(frozen=True, slots=True)
class PlannerTask:
    title: str
    harness: Harness
    prompt: str
    dedupe_key: str
