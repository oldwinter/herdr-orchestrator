from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from herdr_orchestrator.model import Harness, PlannerTask

DEDUPE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


class PlannerOutputError(ValueError):
    pass


def planner_prompt(
    base_prompt: str,
    output_file: Path,
    max_tasks: int,
    compact_catalog: str,
    allowed_harnesses: tuple[Harness, ...],
) -> str:
    allowed_values = "|".join(harness.value for harness in allowed_harnesses)
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
    allowed_values = "|".join(harness.value for harness in allowed_harnesses)
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
    if not path.is_file():
        raise PlannerOutputError("worker_selection_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlannerOutputError("worker_selection_invalid_json") from exc
    if not isinstance(payload, dict) or set(payload) != {"harness"}:
        raise PlannerOutputError("worker_selection_invalid_shape")
    try:
        harness = Harness(_bounded_string(payload, "harness", 32))
    except ValueError as exc:
        raise PlannerOutputError("worker_selection_unsupported") from exc
    if harness not in allowed_harnesses:
        raise PlannerOutputError("worker_selection_not_allowed")
    return harness


def load_planner_tasks(path: Path, *, max_tasks: int) -> tuple[PlannerTask, ...]:
    if not path.is_file():
        raise PlannerOutputError("planner_output_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlannerOutputError("planner_output_invalid_json") from exc
    if not isinstance(payload, dict) or set(payload) != {"tasks"}:
        raise PlannerOutputError("planner_output_invalid_shape")
    rows = payload["tasks"]
    if not isinstance(rows, list) or len(rows) > max_tasks:
        raise PlannerOutputError("planner_tasks_invalid")
    tasks: list[PlannerTask] = []
    dedupe_keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "title",
            "harness",
            "prompt",
            "dedupe_key",
        }:
            raise PlannerOutputError("planner_task_invalid_shape")
        title = _bounded_string(row, "title", 200)
        prompt = _bounded_string(row, "prompt", 50_000)
        dedupe_key = _bounded_string(row, "dedupe_key", 128)
        if not DEDUPE_KEY.fullmatch(dedupe_key):
            raise PlannerOutputError("planner_dedupe_key_invalid")
        if dedupe_key in dedupe_keys:
            raise PlannerOutputError("planner_dedupe_key_duplicate")
        try:
            harness = Harness(_bounded_string(row, "harness", 32))
        except ValueError as exc:
            raise PlannerOutputError("planner_harness_unsupported") from exc
        tasks.append(PlannerTask(title, harness, prompt, dedupe_key))
        dedupe_keys.add(dedupe_key)
    return tuple(tasks)


def _bounded_string(row: dict[str, Any], key: str, maximum: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PlannerOutputError(f"planner_{key}_invalid")
    return value.strip()
