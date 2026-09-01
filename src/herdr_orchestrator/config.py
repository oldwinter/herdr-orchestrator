from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from herdr_orchestrator.catalog import (
    CatalogError,
    load_harness_profiles,
    profile_for_harness,
    profiles_for_workers,
)
from herdr_orchestrator.model import (
    CoordinatorConfig,
    Harness,
    PlacementConfig,
    PlacementMode,
    PlacementTarget,
    PlannerConfig,
    SeedJobConfig,
    StandardizedDeliveryConfig,
    TrackerBackend,
    WayfinderMode,
    WorkerConfig,
    WorkflowConfig,
)

WORKFLOW_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
WORKER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
DEDUPE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


class ConfigError(ValueError):
    pass


def load_workflow(path: str | Path) -> WorkflowConfig:
    workflow_path = Path(path).expanduser().resolve()
    if not workflow_path.is_file():
        raise ConfigError(f"workflow_not_found: {workflow_path}")
    try:
        raw = tomllib.loads(workflow_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"workflow_invalid_toml: {exc}") from exc

    schema_version = _integer(raw, "schema_version", minimum=1, maximum=1)
    name = _string(raw, "name", maximum=64)
    if not WORKFLOW_NAME.fullmatch(name):
        raise ConfigError("workflow_name_invalid")

    base = workflow_path.parent
    workspace = _resolve_path(base, _string(raw, "workspace", maximum=4096))
    if not workspace.is_dir():
        raise ConfigError(f"workspace_not_found: {workspace}")
    state_db = _resolve_path(base, _string(raw, "state_db", maximum=4096))
    profiles_dir = _resolve_path(
        base,
        _optional_string(raw, "profiles_dir", "../profiles/harnesses", maximum=4096),
    )

    coordinator_raw = _table(raw, "coordinator")
    coordinator = CoordinatorConfig(
        poll_seconds=_integer(coordinator_raw, "poll_seconds", minimum=1, maximum=3600),
        max_parallel=_integer(coordinator_raw, "max_parallel", minimum=1, maximum=16),
        lease_seconds=_integer(coordinator_raw, "lease_seconds", minimum=30, maximum=86400),
        max_attempts=_integer(coordinator_raw, "max_attempts", minimum=1, maximum=10),
        agent_timeout_seconds=_integer(
            coordinator_raw,
            "agent_timeout_seconds",
            minimum=10,
            maximum=86400,
        ),
    )
    if coordinator.lease_seconds < coordinator.agent_timeout_seconds + 90:
        raise ConfigError("lease_seconds_must_cover_agent_timeout")

    placement_raw = _optional_table(raw, "placement")
    placement = PlacementConfig(
        mode=_enum_value(
            placement_raw,
            "mode",
            PlacementMode,
            PlacementMode.HYBRID,
        ),
        worktree_root=_optional_path(
            workspace,
            placement_raw,
            "worktree_root",
            ".orchestrator/worktrees",
        ),
    )
    if (
        not placement.worktree_root.is_relative_to(workspace)
        or ".orchestrator" not in placement.worktree_root.parts
    ):
        raise ConfigError("placement_worktree_root_must_be_in_workspace_runtime")

    standardized_delivery = _load_standardized_delivery(workspace, raw)

    planner_raw = _table(raw, "planner")
    planner_output = _resolve_path(
        base,
        _string(planner_raw, "output_file", maximum=4096),
    )
    if not planner_output.is_relative_to(workspace) or ".orchestrator" not in planner_output.parts:
        raise ConfigError("planner_output_must_be_in_workspace_runtime")
    workers, harnesses = _load_workers(raw)

    planner_worker_harnesses = _optional_harness_list(planner_raw, "worker_harnesses")
    if any(harness not in harnesses for harness in planner_worker_harnesses):
        raise ConfigError("planner_worker_harness_has_no_worker")
    planner = PlannerConfig(
        enabled=_boolean(planner_raw, "enabled"),
        harness=_optional_harness(planner_raw, "harness"),
        worker_harnesses=planner_worker_harnesses,
        interval_seconds=_integer(
            planner_raw,
            "interval_seconds",
            minimum=60,
            maximum=86400,
        ),
        prompt_file=_existing_file(base, planner_raw, "prompt_file"),
        output_file=planner_output,
        max_tasks=_integer(planner_raw, "max_tasks", minimum=1, maximum=100),
    )

    try:
        profiles = load_harness_profiles(profiles_dir)
        profiles_for_workers(profiles, workers)
        if planner.harness is not None:
            profile_for_harness(profiles, planner.harness)
    except CatalogError as exc:
        raise ConfigError(str(exc)) from exc

    seed_jobs = _load_seed_jobs(raw, base, harnesses)

    return WorkflowConfig(
        schema_version=schema_version,
        name=name,
        path=workflow_path,
        workspace=workspace,
        state_db=state_db,
        coordinator=coordinator,
        placement=placement,
        standardized_delivery=standardized_delivery,
        planner=planner,
        profiles_dir=profiles_dir,
        profiles=profiles,
        workers=tuple(workers),
        seed_jobs=tuple(seed_jobs),
    )


def _load_standardized_delivery(
    workspace: Path,
    raw: Mapping[str, Any],
) -> StandardizedDeliveryConfig:
    delivery_raw = _optional_table(raw, "standardized_delivery")
    tracker_backend = _enum_value(
        delivery_raw,
        "tracker_backend",
        TrackerBackend,
        TrackerBackend.LOCAL_MARKDOWN,
    )
    tracker_root = _optional_path(
        workspace,
        delivery_raw,
        "tracker_root",
        ".scratch/standardized-delivery",
    )
    artifact_root = _optional_path(
        workspace,
        delivery_raw,
        "artifact_root",
        ".orchestrator/deliveries",
    )
    if not artifact_root.is_relative_to(workspace) or ".orchestrator" not in artifact_root.parts:
        raise ConfigError("delivery_artifact_root_must_be_in_workspace_runtime")
    github_repository = _optional_nullable_string(
        delivery_raw,
        "github_repository",
        maximum=200,
    )
    if tracker_backend is TrackerBackend.GITHUB and github_repository is None:
        raise ConfigError("github_repository_required")
    return StandardizedDeliveryConfig(
        tracker_backend=tracker_backend,
        tracker_root=tracker_root,
        artifact_root=artifact_root,
        github_repository=github_repository,
        wayfinder=_enum_value(
            delivery_raw,
            "wayfinder",
            WayfinderMode,
            WayfinderMode.AUTO,
        ),
        max_parallel=_optional_integer(
            delivery_raw,
            "max_parallel",
            default=3,
            minimum=1,
            maximum=3,
        ),
        review_repair_rounds=_optional_integer(
            delivery_raw,
            "review_repair_rounds",
            default=2,
            minimum=0,
            maximum=2,
        ),
    )


def _load_workers(raw: Mapping[str, Any]) -> tuple[list[WorkerConfig], set[Harness]]:
    worker_rows = _table_list(raw, "workers")
    if not worker_rows:
        raise ConfigError("workers_empty")
    workers: list[WorkerConfig] = []
    worker_names: set[str] = set()
    harnesses: set[Harness] = set()
    for row in worker_rows:
        worker_name = _string(row, "name", maximum=32)
        if not WORKER_NAME.fullmatch(worker_name):
            raise ConfigError(f"worker_name_invalid: {worker_name}")
        harness = _harness(row, "harness")
        if worker_name in worker_names:
            raise ConfigError(f"worker_name_duplicate: {worker_name}")
        if harness in harnesses:
            raise ConfigError(f"worker_harness_duplicate: {harness.value}")
        workers.append(
            WorkerConfig(
                worker_name,
                harness,
                _string_list(row, "capabilities", maximum_items=32),
                _optional_integer(row, "replicas", minimum=1, maximum=16, default=1),
                _optional_placement(row, "placement"),
            )
        )
        worker_names.add(worker_name)
        harnesses.add(harness)
    return workers, harnesses


def _load_seed_jobs(
    raw: Mapping[str, Any],
    base: Path,
    harnesses: set[Harness],
) -> list[SeedJobConfig]:
    seed_jobs: list[SeedJobConfig] = []
    seed_keys: set[str] = set()
    for row in _table_list(raw, "seed_jobs"):
        harness = _harness(row, "harness")
        if harness not in harnesses:
            raise ConfigError(f"seed_harness_has_no_worker: {harness.value}")
        dedupe_key = _string(row, "dedupe_key", maximum=128)
        if not DEDUPE_KEY.fullmatch(dedupe_key):
            raise ConfigError(f"dedupe_key_invalid: {dedupe_key}")
        if dedupe_key in seed_keys:
            raise ConfigError(f"seed_dedupe_key_duplicate: {dedupe_key}")
        seed_jobs.append(
            SeedJobConfig(
                title=_string(row, "title", maximum=200),
                harness=harness,
                prompt_file=_existing_file(base, row, "prompt_file"),
                dedupe_key=dedupe_key,
                placement=_optional_placement(row, "placement"),
            )
        )
        seed_keys.add(dedupe_key)
    return seed_jobs


def _resolve_path(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key}_must_be_table")
    return value


def _optional_table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key}_must_be_table")
    return value


def _table_list(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError(f"{key}_must_be_table_array")
    return value


def _string(data: Mapping[str, Any], key: str, *, maximum: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ConfigError(f"{key}_must_be_non_empty_string")
    return value.strip()


def _optional_string(
    data: Mapping[str, Any],
    key: str,
    default: str,
    *,
    maximum: int,
) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ConfigError(f"{key}_must_be_non_empty_string")
    return value.strip()


def _string_list(
    data: Mapping[str, Any],
    key: str,
    *,
    maximum_items: int,
) -> tuple[str, ...]:
    value = data.get(key, [])
    if (
        not isinstance(value, list)
        or len(value) > maximum_items
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ConfigError(f"{key}_must_be_string_array")
    return tuple(item.strip() for item in value)


def _optional_integer(
    data: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ConfigError(f"{key}_must_be_integer_{minimum}_{maximum}")
    return value


def _integer(
    data: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ConfigError(f"{key}_must_be_integer_{minimum}_{maximum}")
    return value


def _boolean(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{key}_must_be_boolean")
    return value


def _harness(data: Mapping[str, Any], key: str) -> Harness:
    value = _string(data, key, maximum=32)
    try:
        return Harness(value)
    except ValueError as exc:
        raise ConfigError(f"unsupported_harness: {value}") from exc


def _optional_harness(data: Mapping[str, Any], key: str) -> Harness | None:
    value = data.get(key, "auto")
    if not isinstance(value, str) or not value.strip() or len(value) > 32:
        raise ConfigError(f"{key}_must_be_harness_or_auto")
    value = value.strip()
    if value == "auto":
        return None
    try:
        return Harness(value)
    except ValueError as exc:
        raise ConfigError(f"unsupported_harness: {value}") from exc


def _optional_harness_list(
    data: Mapping[str, Any],
    key: str,
) -> tuple[Harness, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key}_must_be_non_empty_harness_list")
    harnesses: list[Harness] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{key}_must_be_non_empty_harness_list")
        try:
            harness = Harness(item.strip())
        except ValueError as exc:
            raise ConfigError(f"unsupported_harness: {item}") from exc
        if harness in harnesses:
            raise ConfigError(f"{key}_duplicate: {harness.value}")
        harnesses.append(harness)
    return tuple(harnesses)


def _optional_placement(
    data: Mapping[str, Any],
    key: str,
) -> PlacementTarget | None:
    value = data.get(key, "auto")
    if not isinstance(value, str):
        raise ConfigError(f"{key}_must_be_placement_or_auto")
    if value == "auto":
        return None
    try:
        return PlacementTarget(value)
    except ValueError as exc:
        raise ConfigError(f"{key}_unsupported: {value}") from exc


def _optional_nullable_string(
    data: Mapping[str, Any],
    key: str,
    *,
    maximum: int,
) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ConfigError(f"{key}_must_be_non_empty_string")
    return value.strip()


def _optional_path(
    workspace: Path,
    data: Mapping[str, Any],
    key: str,
    default: str,
) -> Path:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ConfigError(f"{key}_must_be_non_empty_string")
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (workspace / candidate).resolve()


def _enum_value[EnumValue: StrEnum](
    data: Mapping[str, Any],
    key: str,
    enum_type: type[EnumValue],
    default: EnumValue,
) -> EnumValue:
    value = data.get(key, default.value)
    if not isinstance(value, str):
        raise ConfigError(f"{key}_must_be_string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ConfigError(f"{key}_unsupported: {value}") from exc


def _existing_file(base: Path, data: Mapping[str, Any], key: str) -> Path:
    path = _resolve_path(base, _string(data, key, maximum=4096))
    if not path.is_file():
        raise ConfigError(f"{key}_not_found: {path}")
    return path
