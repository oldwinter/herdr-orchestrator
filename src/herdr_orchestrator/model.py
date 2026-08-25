from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


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


class PlacementMode(StrEnum):
    HYBRID = "hybrid"
    TAB = "tab"
    PANE = "pane"
    WORKTREE = "worktree"


class PlacementTarget(StrEnum):
    TAB = "tab"
    PANE = "pane"
    WORKTREE = "worktree"


class ReceiptKind(StrEnum):
    OUTPUT_PREFIX = "output-prefix"
    FILE = "file"


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


@dataclass(frozen=True, slots=True)
class TaskReceipt:
    kind: ReceiptKind
    value: str


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


@dataclass(frozen=True, slots=True)
class DispatchContext:
    placement: PlacementTarget
    title: str
    task_key: str
    batch_key: str | None = None
    worktree_root: Path | None = None
    receipt: TaskReceipt | None = None
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class PlannerTask:
    title: str
    harness: Harness
    prompt: str
    dedupe_key: str
