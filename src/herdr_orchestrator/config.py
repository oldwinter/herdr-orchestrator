from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from herdr_orchestrator.executor_config import (
    EXECUTOR_FIELDS,
    EXECUTOR_TABLE_TO_KIND,
    PROGRAMMABLE_FIELDS,
    REGISTERED_EXECUTORS,
    STATIC_CHECK_FIELDS,
    ExecutorConfig,
    ExecutorKind,
)
from herdr_orchestrator.model import (
    CoordinatorConfig,
    Harness,
    PlannerConfig,
    SeedJobConfig,
    WorkerConfig,
    WorkflowConfig,
)
from herdr_orchestrator.research_executor import (
    ResearchConfig,
    ResearchEvidenceError,
    ResearchInputError,
)

WORKFLOW_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
WORKER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
DEDUPE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
ROLE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")

V1_V2_FIELDS = frozenset(
    {
        "runtime_dir",
        "executor",
        "research",
        "research-synthesis",
        "review",
        "code-review-gate",
        "delivery",
        "software-delivery",
        "incident",
        "incident-response",
    }
)
V2_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "workspace",
        "state_db",
        "runtime_dir",
        "coordinator",
        "executor",
        "workers",
        "research",
        "research-synthesis",
        "review",
        "code-review-gate",
        "delivery",
        "software-delivery",
        "incident",
        "incident-response",
    }
)


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

    schema_version = _schema_version(raw)
    if schema_version == 1:
        return _load_v1(workflow_path, raw)
    if schema_version == 2:
        return _load_v2(workflow_path, raw)
    raise ConfigError(f"unsupported_schema_version: {schema_version}")


def _load_v1(workflow_path: Path, raw: Mapping[str, Any]) -> WorkflowConfig:
    v2_field = _find_key(raw, V1_V2_FIELDS)
    if v2_field is not None:
        raise ConfigError(f"schema_mismatch: v1_workflow_contains_v2_field:{v2_field}")

    name = _string(raw, "name", maximum=64)
    if not WORKFLOW_NAME.fullmatch(name):
        raise ConfigError("workflow_name_invalid")

    base = workflow_path.parent
    workspace = _resolve_path(base, _string(raw, "workspace", maximum=4096))
    if not workspace.is_dir():
        raise ConfigError(f"workspace_not_found: {workspace}")
    state_db = _resolve_path(base, _string(raw, "state_db", maximum=4096))

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
            maximum=3600,
        ),
    )
    if coordinator.lease_seconds < coordinator.agent_timeout_seconds + 90:
        raise ConfigError("lease_seconds_must_cover_agent_timeout")

    planner_raw = _table(raw, "planner")
    planner_output = _resolve_path(
        base,
        _string(planner_raw, "output_file", maximum=4096),
    )
    if not planner_output.is_relative_to(workspace) or ".orchestrator" not in planner_output.parts:
        raise ConfigError("planner_output_must_be_in_workspace_runtime")
    planner = PlannerConfig(
        enabled=_boolean(planner_raw, "enabled"),
        harness=_harness(planner_raw, "harness"),
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
        capabilities = _string_list(row, "capabilities", maximum_items=32)
        replicas = _optional_integer(row, "replicas", minimum=1, maximum=16, default=1)
        workers.append(WorkerConfig(worker_name, harness, capabilities, replicas))
        worker_names.add(worker_name)
        harnesses.add(harness)

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
            )
        )
        seed_keys.add(dedupe_key)

    return WorkflowConfig(
        schema_version=1,
        name=name,
        path=workflow_path,
        workspace=workspace,
        state_db=state_db,
        coordinator=coordinator,
        planner=planner,
        workers=tuple(workers),
        seed_jobs=tuple(seed_jobs),
    )


def _load_v2(workflow_path: Path, raw: Mapping[str, Any]) -> WorkflowConfig:
    for key in raw:
        if key in PROGRAMMABLE_FIELDS:
            raise ConfigError(f"configuration_rejected: {key}")
        if key not in V2_TOP_LEVEL_FIELDS:
            raise ConfigError(f"workflow_unknown_field: {key}")

    name = _string(raw, "name", maximum=64)
    if not WORKFLOW_NAME.fullmatch(name):
        raise ConfigError("workflow_name_invalid")

    base = workflow_path.parent
    workspace = _resolve_path(base, _string(raw, "workspace", maximum=4096))
    if not workspace.is_dir():
        raise ConfigError(f"workspace_not_found: {workspace}")
    state_db = _resolve_path(base, _string(raw, "state_db", maximum=4096))
    runtime_dir = _resolve_path(base, _string(raw, "runtime_dir", maximum=4096))
    runtime_boundary = (workspace / ".orchestrator").resolve()
    if not runtime_dir.is_relative_to(runtime_boundary):
        raise ConfigError("runtime_dir_must_be_in_workspace_runtime")

    coordinator = _load_coordinator(_table(raw, "coordinator"))
    workers = _load_workers(raw, schema_version=2)
    executor = _load_executor(raw, workers)

    return WorkflowConfig(
        schema_version=2,
        name=name,
        path=workflow_path,
        workspace=workspace,
        state_db=state_db,
        coordinator=coordinator,
        planner=None,
        workers=workers,
        seed_jobs=(),
        runtime_dir=runtime_dir,
        executor=executor,
    )


def _schema_version(raw: Mapping[str, Any]) -> int:
    if "schema_version" not in raw:
        raise ConfigError("schema_version_missing")
    value = raw["schema_version"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError("schema_version_must_be_integer")
    if value not in {1, 2}:
        raise ConfigError(f"unsupported_schema_version: {value}")
    return value


def _load_coordinator(data: Mapping[str, Any]) -> CoordinatorConfig:
    _reject_unknown_keys(
        data,
        {
            "poll_seconds",
            "max_parallel",
            "lease_seconds",
            "max_attempts",
            "agent_timeout_seconds",
        },
        "coordinator_unknown_field",
    )
    coordinator = CoordinatorConfig(
        poll_seconds=_integer(data, "poll_seconds", minimum=1, maximum=3600),
        max_parallel=_integer(data, "max_parallel", minimum=1, maximum=16),
        lease_seconds=_integer(data, "lease_seconds", minimum=30, maximum=86400),
        max_attempts=_integer(data, "max_attempts", minimum=1, maximum=10),
        agent_timeout_seconds=_integer(
            data,
            "agent_timeout_seconds",
            minimum=10,
            maximum=3600,
        ),
    )
    if coordinator.lease_seconds < coordinator.agent_timeout_seconds + 90:
        raise ConfigError("lease_seconds_must_cover_agent_timeout")
    return coordinator


def _load_workers(raw: Mapping[str, Any], *, schema_version: int) -> tuple[WorkerConfig, ...]:
    worker_rows = _table_list(raw, "workers")
    if not worker_rows:
        raise ConfigError("workers_empty")
    workers: list[WorkerConfig] = []
    worker_names: set[str] = set()
    harnesses: set[Harness] = set()
    allowed_fields = {"name", "harness", "capabilities", "replicas"}
    for row in worker_rows:
        if schema_version == 2:
            _reject_unknown_keys(row, allowed_fields, "worker_unknown_field")
        worker_name = _string(row, "name", maximum=32)
        if not WORKER_NAME.fullmatch(worker_name):
            raise ConfigError(f"worker_name_invalid: {worker_name}")
        harness = _harness(row, "harness")
        if worker_name in worker_names:
            raise ConfigError(f"worker_name_duplicate: {worker_name}")
        if harness in harnesses:
            raise ConfigError(f"worker_harness_duplicate: {harness.value}")
        capabilities = _string_list(row, "capabilities", maximum_items=32)
        replicas = _optional_integer(row, "replicas", minimum=1, maximum=16, default=1)
        workers.append(WorkerConfig(worker_name, harness, capabilities, replicas))
        worker_names.add(worker_name)
        harnesses.add(harness)
    return tuple(workers)


def _load_executor(
    raw: Mapping[str, Any],
    workers: tuple[WorkerConfig, ...],
) -> ExecutorConfig:
    if "executor" not in raw:
        raise ConfigError("executor_missing")
    executor_raw = _table(raw, "executor")
    if "kind" not in executor_raw:
        raise ConfigError("executor_kind_missing")
    kind_value = _string(executor_raw, "kind", maximum=64)
    try:
        kind = ExecutorKind(kind_value)
    except ValueError as exc:
        raise ConfigError(f"unsupported_executor: {kind_value}") from exc
    if kind not in REGISTERED_EXECUTORS:
        raise ConfigError(f"unsupported_executor: {kind_value}")

    nested_tables: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in executor_raw.items():
        if key == "kind":
            continue
        if key in PROGRAMMABLE_FIELDS:
            raise ConfigError(f"configuration_rejected: {key}")
        if key not in EXECUTOR_TABLE_TO_KIND:
            raise ConfigError(f"executor_unknown_field: {key}")
        if not isinstance(value, dict):
            raise ConfigError(f"executor_table_must_be_table: {key}")
        nested_tables.append((key, value))

    top_level_tables: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in raw.items():
        if key not in EXECUTOR_TABLE_TO_KIND:
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"executor_table_must_be_table: {key}")
        top_level_tables.append((key, value))
    declared_tables = top_level_tables + nested_tables
    if len(declared_tables) > 1:
        raise ConfigError("executor_multiple_configurations")
    if declared_tables:
        table_name, settings = declared_tables[0]
        declared_kind = EXECUTOR_TABLE_TO_KIND[table_name]
        if declared_kind is not kind:
            raise ConfigError("executor_mismatch")
    else:
        settings = {}

    _validate_executor_settings(
        kind,
        settings,
        worker_capabilities={
            worker.name: frozenset(worker.capabilities)
            for worker in workers
        },
    )
    return ExecutorConfig(kind=kind, settings=_copy_mapping(settings))


def _validate_executor_settings(
    kind: ExecutorKind,
    settings: Mapping[str, Any],
    *,
    worker_capabilities: Mapping[str, frozenset[str]],
) -> None:
    allowed = EXECUTOR_FIELDS[kind]
    for key, value in settings.items():
        if key in PROGRAMMABLE_FIELDS:
            raise ConfigError(f"configuration_rejected: {key}")
        if key not in allowed:
            raise ConfigError(f"executor_unknown_field: {key}")
        _validate_setting_value(key, value)
    if kind is ExecutorKind.RESEARCH_SYNTHESIS:
        try:
            ResearchConfig.from_mapping(settings)
        except (ResearchInputError, ResearchEvidenceError) as exc:
            raise ConfigError(str(exc)) from exc
    _validate_role_references(settings, worker_capabilities)


def _validate_setting_value(key: str, value: Any) -> None:
    if key == "checks":
        if not isinstance(value, list):
            raise ConfigError("checks_must_be_table_array")
        for check in value:
            if not isinstance(check, dict):
                raise ConfigError("check_must_be_table")
            _validate_static_check(check)
        return
    _reject_programmable_nested(value)


def _validate_static_check(data: Mapping[str, Any]) -> None:
    for key in data:
        if key in PROGRAMMABLE_FIELDS:
            raise ConfigError(f"configuration_rejected: {key}")
        if key not in STATIC_CHECK_FIELDS:
            raise ConfigError(f"static_check_unknown_field: {key}")
    argv = data.get("argv")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise ConfigError("static_check_argv_must_be_non_empty_string_array")
    if "timeout_seconds" in data:
        value = data["timeout_seconds"]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 3600:
            raise ConfigError("static_check_timeout_must_be_integer_1_3600")
    if "max_output_bytes" in data:
        value = data["max_output_bytes"]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1_000_000:
            raise ConfigError("static_check_max_output_bytes_must_be_integer_1_1000000")
    for key in ("id", "name", "cwd"):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise ConfigError(f"static_check_{key}_must_be_non_empty_string")
    if "environment" in data:
        environment = data["environment"]
        if (
            not isinstance(environment, dict)
            or not all(
                isinstance(env_key, str)
                and isinstance(env_value, str)
                for env_key, env_value in environment.items()
            )
        ):
            raise ConfigError("static_check_environment_must_be_string_table")


def _validate_role_references(
    settings: Mapping[str, Any],
    worker_capabilities: Mapping[str, frozenset[str]],
) -> None:
    roles = settings.get("roles")
    if roles is None:
        return
    if not isinstance(roles, dict):
        raise ConfigError("executor_roles_must_be_table")
    for role, worker_name in roles.items():
        if not isinstance(role, str) or not ROLE_NAME.fullmatch(role):
            raise ConfigError("executor_role_name_invalid")
        required_capabilities: tuple[str, ...] = ()
        if isinstance(worker_name, dict):
            _reject_unknown_keys(
                worker_name,
                {"worker", "capabilities"},
                "executor_role_unknown_field",
            )
            required_worker = worker_name.get("worker")
            required = worker_name.get("capabilities", [])
            if not isinstance(required, list) or not all(
                isinstance(capability, str) and capability.strip()
                for capability in required
            ):
                raise ConfigError(f"executor_role_capabilities_invalid: {role}")
            required_capabilities = tuple(required)
            worker_name = required_worker
        if not isinstance(worker_name, str) or not WORKER_NAME.fullmatch(worker_name):
            raise ConfigError(f"executor_role_worker_invalid: {role}")
        if worker_name not in worker_capabilities:
            raise ConfigError(f"executor_role_worker_not_found: {worker_name}")
        if not set(required_capabilities).issubset(worker_capabilities[worker_name]):
            raise ConfigError(f"executor_role_capabilities_missing: {role}")


def _reject_programmable_nested(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in PROGRAMMABLE_FIELDS:
                raise ConfigError(f"configuration_rejected: {key}")
            _reject_programmable_nested(nested)
    elif isinstance(value, list):
        for item in value:
            _reject_programmable_nested(item)


def _find_key(value: Any, keys: frozenset[str]) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in keys:
                return key
            found = _find_key(nested, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key(item, keys)
            if found is not None:
                return found
    return None


def _reject_unknown_keys(
    data: Mapping[str, Any],
    allowed: set[str] | frozenset[str],
    error_prefix: str,
) -> None:
    for key in data:
        if key in PROGRAMMABLE_FIELDS:
            raise ConfigError(f"configuration_rejected: {key}")
        if key not in allowed:
            raise ConfigError(f"{error_prefix}: {key}")


def _copy_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    # Keep TOML's public list/table shapes while detaching the settings from
    # the parser's temporary object.
    return {
        str(key): _copy_value(item)
        for key, item in value.items()
    }


def _copy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _copy_mapping(value)
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    return value


def _resolve_path(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
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
    if key not in data:
        return default
    return _integer(data, key, minimum=minimum, maximum=maximum)


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


def _existing_file(base: Path, data: Mapping[str, Any], key: str) -> Path:
    path = _resolve_path(base, _string(data, key, maximum=4096))
    if not path.is_file():
        raise ConfigError(f"{key}_not_found: {path}")
    return path
