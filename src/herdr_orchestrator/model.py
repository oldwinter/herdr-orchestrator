from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Harness(StrEnum):
    DROID = "droid"
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


@dataclass(frozen=True, slots=True)
class CoordinatorConfig:
    poll_seconds: int
    max_parallel: int
    lease_seconds: int
    max_attempts: int
    agent_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    enabled: bool
    harness: Harness
    interval_seconds: int
    prompt_file: Path
    output_file: Path
    max_tasks: int


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    name: str
    harness: Harness
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SeedJobConfig:
    title: str
    harness: Harness
    prompt_file: Path
    dedupe_key: str


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    schema_version: int
    name: str
    path: Path
    workspace: Path
    state_db: Path
    coordinator: CoordinatorConfig
    planner: PlannerConfig
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


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    agent_name: str
    state: AgentState
    member_reused: bool
    pane_id: str | None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PlannerTask:
    title: str
    harness: Harness
    prompt: str
    dedupe_key: str
