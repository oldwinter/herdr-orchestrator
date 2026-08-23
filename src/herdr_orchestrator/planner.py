from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from herdr_orchestrator.model import Harness, PlannerTask

DEDUPE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


class PlannerOutputError(ValueError):
    pass


def planner_prompt(base_prompt: str, output_file: Path, max_tasks: int) -> str:
    return (
        f"{base_prompt.strip()}\n\n"
        "唯一允许写入的文件：\n"
        f"{output_file}\n\n"
        f"最多 {max_tasks} 项。文件必须是 UTF-8 JSON，且严格符合：\n"
        '{"tasks":[{"title":"...","harness":"'
        + "|".join(item.value for item in Harness)
        + '",'
        '"prompt":"...","dedupe_key":"..."}]}\n'
        "不要输出 shell command 字段。写完文件后只回复任务数量。"
    )


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
