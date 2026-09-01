from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from herdr_orchestrator.model import Harness, PlannerTask

DEDUPE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
MAX_PLANNER_TASKS = 100
MAX_PLANNER_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_WORKER_SELECTION_OUTPUT_BYTES = 4 * 1024
_TASK_KEYS = frozenset({"title", "harness", "prompt", "dedupe_key"})


class PlannerOutputError(ValueError):
    pass


def planner_prompt(
    base_prompt: str,
    output_file: Path,
    max_tasks: int,
    compact_catalog: str,
    allowed_harnesses: tuple[Harness, ...],
) -> str:
    _validate_max_tasks(max_tasks)
    _validate_prompt_text(base_prompt, "planner_base_prompt")
    _validate_prompt_text(compact_catalog, "planner_catalog")
    _validate_prompt_path(output_file, "planner_output")
    allowed_values = _allowed_values(allowed_harnesses, "planner")
    return (
        f"{base_prompt.strip()}\n\n"
        "# Available harness catalog\n\n"
        f"{compact_catalog}\n\n"
        "先根据 summary、best_for、avoid_for、strengths 和 traits 为每个子任务选择 "
        "harness。此处只提供紧凑 catalog；coordinator 会在执行前按需加载所选 harness "
        "的完整 profile。不得选择 catalog 之外的 harness。\n\n"
        "唯一允许写入的文件：\n"
        f"{output_file}\n\n"
        f"最多 {max_tasks} 项。文件必须是 UTF-8 JSON，且严格符合：\n"
        f'{{"tasks":[{{"title":"...","harness":"{allowed_values}",'
        '"prompt":"...","dedupe_key":"..."}]}\n'
        "不要输出 shell command 字段。写完文件后只回复任务数量。"
    )


def worker_selection_prompt(
    task_prompt: str,
    output_file: Path,
    compact_catalog: str,
    allowed_harnesses: tuple[Harness, ...],
) -> str:
    _validate_prompt_text(task_prompt, "worker_selection_task_prompt")
    _validate_prompt_text(compact_catalog, "worker_selection_catalog")
    _validate_prompt_path(output_file, "worker_selection_output")
    allowed_values = _allowed_values(allowed_harnesses, "worker_selection")
    return (
        "你是受限 harness router。只为下面这个任务选择一个最合适的执行 harness，"
        "不要执行任务本身。\n\n"
        "# Available harness catalog\n\n"
        f"{compact_catalog}\n\n"
        "# Task to route\n\n"
        f"{task_prompt.strip()}\n\n"
        "唯一允许写入的文件：\n"
        f"{output_file}\n\n"
        "文件必须是 UTF-8 JSON，且严格符合：\n"
        f'{{"harness":"{allowed_values}"}}\n'
        "不得选择 catalog 之外的 harness，不要输出其他字段。写完文件后只回复所选 harness。"
    )


def load_worker_selection(
    path: Path,
    *,
    allowed_harnesses: tuple[Harness, ...],
) -> Harness:
    allowed = _normalize_allowed_harnesses(allowed_harnesses, "worker_selection")
    payload = _load_json(
        path,
        artifact="worker_selection",
        maximum_bytes=MAX_WORKER_SELECTION_OUTPUT_BYTES,
    )
    if not isinstance(payload, dict) or set(payload) != {"harness"}:
        raise PlannerOutputError("worker_selection_invalid_shape")
    value = _bounded_string(payload, "harness", 32, prefix="worker_selection")
    try:
        harness = Harness(value)
    except ValueError as exc:
        raise PlannerOutputError("worker_selection_unsupported") from exc
    if harness not in allowed:
        raise PlannerOutputError("worker_selection_not_allowed")
    return harness


def load_planner_tasks(
    path: Path,
    *,
    max_tasks: int,
) -> tuple[PlannerTask, ...]:
    _validate_max_tasks(max_tasks)
    payload = _load_json(
        path,
        artifact="planner_output",
        maximum_bytes=MAX_PLANNER_OUTPUT_BYTES,
    )
    if not isinstance(payload, dict) or set(payload) != {"tasks"}:
        raise PlannerOutputError("planner_output_invalid_shape")
    rows = payload["tasks"]
    if not isinstance(rows, list) or len(rows) > max_tasks:
        raise PlannerOutputError("planner_tasks_invalid")
    tasks: list[PlannerTask] = []
    dedupe_keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _TASK_KEYS:
            raise PlannerOutputError("planner_task_invalid_shape")
        title = _bounded_string(row, "title", 200, prefix="planner")
        prompt = _bounded_string(row, "prompt", 50_000, prefix="planner")
        dedupe_key = _bounded_string(row, "dedupe_key", 128, prefix="planner")
        if not DEDUPE_KEY.fullmatch(dedupe_key):
            raise PlannerOutputError("planner_dedupe_key_invalid")
        if dedupe_key in dedupe_keys:
            raise PlannerOutputError("planner_dedupe_key_duplicate")
        harness_value = _bounded_string(row, "harness", 32, prefix="planner")
        try:
            harness = Harness(harness_value)
        except ValueError as exc:
            raise PlannerOutputError("planner_harness_unsupported") from exc
        tasks.append(PlannerTask(title, harness, prompt, dedupe_key))
        dedupe_keys.add(dedupe_key)
    return tuple(tasks)


def _bounded_string(row: dict[str, Any], key: str, maximum: int, *, prefix: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise PlannerOutputError(f"{prefix}_{key}_invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PlannerOutputError(f"{prefix}_{key}_invalid") from exc
    return value.strip()


def _load_json(path: Path, *, artifact: str, maximum_bytes: int) -> Any:
    _validate_existing_output_path(path, artifact)
    try:
        with _open_without_following_final_symlink(path, artifact) as stream:
            data = stream.read(maximum_bytes + 1)
    except FileNotFoundError as exc:
        raise PlannerOutputError(f"{artifact}_missing") from exc
    except PlannerOutputError:
        raise
    except (OSError, ValueError) as exc:
        raise PlannerOutputError(f"{artifact}_unreadable") from exc
    if len(data) > maximum_bytes:
        raise PlannerOutputError(f"{artifact}_too_large")
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=lambda pairs: _reject_duplicate_keys(pairs, artifact),
            parse_constant=lambda value: _reject_json_constant(value, artifact),
        )
    except PlannerOutputError:
        raise
    except UnicodeError as exc:
        raise PlannerOutputError(f"{artifact}_unreadable") from exc
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PlannerOutputError(f"{artifact}_invalid_json") from exc


def _open_without_following_final_symlink(path: Path, artifact: str) -> BinaryIO:
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    descriptor = os.open(path, flags)
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise PlannerOutputError(f"{artifact}_unreadable")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def _validate_existing_output_path(path: Path, artifact: str) -> None:
    _validate_prompt_path(path, artifact)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise PlannerOutputError(f"{artifact}_missing") from None
    except OSError as exc:
        raise PlannerOutputError(f"{artifact}_unreadable") from exc
    if stat.S_ISLNK(info.st_mode):
        raise PlannerOutputError(f"{artifact}_path_invalid")
    if not stat.S_ISREG(info.st_mode):
        raise PlannerOutputError(f"{artifact}_unreadable")


def _validate_prompt_path(path: Path, artifact: str) -> None:
    if not isinstance(path, Path):
        raise PlannerOutputError(f"{artifact}_path_invalid")
    rendered = str(path)
    if (
        not rendered
        or ".." in path.parts
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in rendered)
    ):
        raise PlannerOutputError(f"{artifact}_path_invalid")
    for parent in (path, *path.parents):
        try:
            if parent.is_symlink():
                raise PlannerOutputError(f"{artifact}_path_invalid")
        except OSError as exc:
            raise PlannerOutputError(f"{artifact}_unreadable") from exc


def _validate_prompt_text(value: str, key: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise PlannerOutputError(f"{key}_invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PlannerOutputError(f"{key}_invalid") from exc


def _validate_max_tasks(max_tasks: int) -> None:
    if (
        isinstance(max_tasks, bool)
        or not isinstance(max_tasks, int)
        or not 1 <= max_tasks <= MAX_PLANNER_TASKS
    ):
        raise PlannerOutputError("planner_max_tasks_invalid")


def _normalize_allowed_harnesses(
    allowed_harnesses: Iterable[Harness],
    prefix: str,
) -> tuple[Harness, ...]:
    try:
        values = tuple(allowed_harnesses)
    except TypeError as exc:
        raise PlannerOutputError(f"{prefix}_harnesses_invalid") from exc
    if not values or any(not isinstance(value, Harness) for value in values):
        raise PlannerOutputError(f"{prefix}_harnesses_invalid")
    if len(set(values)) != len(values):
        raise PlannerOutputError(f"{prefix}_harnesses_invalid")
    return values


def _allowed_values(allowed_harnesses: Iterable[Harness], prefix: str) -> str:
    return "|".join(
        harness.value for harness in _normalize_allowed_harnesses(allowed_harnesses, prefix)
    )


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
    artifact: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise PlannerOutputError(f"{artifact}_duplicate_key")
        payload[key] = value
    return payload


def _reject_json_constant(_value: str, artifact: str) -> NoReturn:
    raise PlannerOutputError(f"{artifact}_invalid_json")
