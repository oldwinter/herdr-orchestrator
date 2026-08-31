from __future__ import annotations

import errno
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from herdr_orchestrator.model import PlacementMode, PlacementTarget

HARD_READ_ONLY_SIGNALS = (
    "只读",
    "不得修改",
    "不要修改",
    "do not modify",
    "read only",
    "read-only",
)
READ_SIGNALS = (
    "inspect",
    "review",
    "audit",
)
WRITE_SIGNALS = (
    "实现",
    "修复",
    "修改",
    "创建文件",
    "写入",
    "implement",
    "fix",
    "modify",
    "edit",
    "write",
    "create file",
    "refactor",
)
LABEL_CHAR = re.compile(r"[^a-z0-9]+")
PLACEMENT_KEYS = {"placement", "rationale"}
TOPOLOGY_OUTPUT_MAX_BYTES = 32 * 1024 * 1024


class TopologyDecisionError(ValueError):
    pass


def static_placement(
    mode: PlacementMode,
    title: str,
    prompt: str,
    *,
    override: PlacementTarget | None = None,
    worker_default: PlacementTarget | None = None,
    supports_worktree: bool = True,
) -> PlacementTarget | None:
    if not isinstance(mode, PlacementMode):
        raise TopologyDecisionError("topology_mode_invalid")
    _validate_git_capability(supports_worktree)
    if override is not None:
        return _validate_worktree(override, supports_worktree)
    if worker_default is not None:
        return _validate_worktree(worker_default, supports_worktree)
    if mode is not PlacementMode.HYBRID:
        return _validate_worktree(PlacementTarget(mode.value), supports_worktree)

    content = f"{title}\n{prompt}".casefold()
    if any(_contains_signal(content, signal) for signal in HARD_READ_ONLY_SIGNALS):
        return PlacementTarget.PANE
    if any(_contains_signal(content, signal) for signal in WRITE_SIGNALS):
        return PlacementTarget.WORKTREE if supports_worktree else PlacementTarget.TAB
    if any(_contains_signal(content, signal) for signal in READ_SIGNALS):
        return PlacementTarget.PANE
    return None


def topology_decision_prompt(
    title: str,
    prompt: str,
    output_file: Path,
    *,
    supports_worktree: bool,
) -> str:
    _validate_git_capability(supports_worktree)
    _validate_prompt_text(title)
    _validate_prompt_text(prompt)
    _validate_output_path(output_file)
    allowed = ["tab", "pane"]
    if supports_worktree:
        allowed.append("worktree")
    choices = "|".join(allowed)
    worktree_guidance = (
        "\n- worktree: repository-writing work that needs an isolated branch and checkout."
        if supports_worktree
        else ""
    )
    return f"""
Choose the Herdr execution topology for one already accepted local task.

- pane: read-only or cooperative work that may share the checkout and a batch tab.
- tab: work needing a full terminal while still sharing the checkout.
{worktree_guidance}

Do not execute the task. Choose only from: {", ".join(allowed)}.

Title:
{title.strip()}

Task:
{prompt.strip()}

Write only this UTF-8 JSON file:
{output_file}

Exact schema:
{{"placement":"{choices}","rationale":"..."}}
""".strip()


def load_topology_decision(
    path: Path,
    *,
    supports_worktree: bool,
) -> PlacementTarget:
    _validate_git_capability(supports_worktree)
    _validate_output_path(path)
    try:
        payload = json.loads(
            _read_controller_output(path),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except TopologyDecisionError:
        raise
    except json.JSONDecodeError as exc:
        raise TopologyDecisionError("topology_output_invalid_json") from exc
    except RecursionError as exc:
        raise TopologyDecisionError("topology_output_invalid_json") from exc
    if not isinstance(payload, dict) or set(payload) != PLACEMENT_KEYS:
        raise TopologyDecisionError("topology_output_invalid_shape")
    rationale = payload.get("rationale")
    if (
        not isinstance(rationale, str)
        or not rationale.strip()
        or len(rationale) > 2_000
        or "\x00" in rationale
    ):
        raise TopologyDecisionError("topology_rationale_invalid")
    placement = payload.get("placement")
    if not isinstance(placement, str):
        raise TopologyDecisionError("topology_placement_invalid")
    try:
        target = PlacementTarget(placement)
    except (TypeError, ValueError) as exc:
        raise TopologyDecisionError("topology_placement_invalid") from exc
    return _validate_worktree(target, supports_worktree)


def short_display_label(title: str, *, fallback: str, maximum: int = 32) -> str:
    normalized = " ".join(title.split()).strip()
    if not normalized:
        normalized = fallback
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 1].rstrip() + "…"


def stable_slug(value: str, *, fallback: str = "task", maximum: int = 36) -> str:
    normalized = LABEL_CHAR.sub("-", value.casefold()).strip("-") or fallback
    return normalized[:maximum].rstrip("-") or fallback


def _validate_worktree(
    target: PlacementTarget,
    supports_worktree: bool,
) -> PlacementTarget:
    if not isinstance(target, PlacementTarget):
        raise TopologyDecisionError("topology_placement_invalid")
    _validate_git_capability(supports_worktree)
    if target is PlacementTarget.WORKTREE and not supports_worktree:
        raise TopologyDecisionError("topology_worktree_requires_git")
    return target


def _contains_signal(content: str, signal: str) -> bool:
    if not signal.isascii():
        return signal in content
    return re.search(rf"(?<!\w){re.escape(signal)}", content) is not None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise TopologyDecisionError("topology_output_duplicate_key")
        payload[key] = value
    return payload


def _read_controller_output(path: Path) -> str:
    _validate_output_path(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        with _open_controller_output(path, nofollow) as stream:
            content = stream.read(TOPOLOGY_OUTPUT_MAX_BYTES + 1)
    except FileNotFoundError as exc:
        raise TopologyDecisionError("topology_output_missing") from exc
    except TopologyDecisionError:
        raise
    except (OSError, ValueError) as exc:
        raise TopologyDecisionError("topology_output_unreadable") from exc
    if len(content) > TOPOLOGY_OUTPUT_MAX_BYTES:
        raise TopologyDecisionError("topology_output_too_large")
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise TopologyDecisionError("topology_output_unreadable") from exc


def _open_controller_output(path: Path, nofollow: int) -> BinaryIO:
    flags = os.O_RDONLY | nofollow
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if nofollow and exc.errno == errno.ELOOP:
            raise TopologyDecisionError("topology_output_path_invalid") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TopologyDecisionError("topology_output_unreadable")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def _validate_output_path(path: Path) -> None:
    if not isinstance(path, Path):
        raise TopologyDecisionError("topology_output_path_invalid")
    rendered = str(path)
    if (
        not rendered
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 for char in rendered)
    ):
        raise TopologyDecisionError("topology_output_path_invalid")
    for parent in (path, *path.parents):
        try:
            if parent.is_symlink():
                raise TopologyDecisionError("topology_output_path_invalid")
        except OSError as exc:
            raise TopologyDecisionError("topology_output_unreadable") from exc


def _validate_prompt_text(value: str) -> None:
    if not isinstance(value, str):
        raise TopologyDecisionError("topology_prompt_invalid")
    if any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in value):
        raise TopologyDecisionError("topology_prompt_invalid")


def _validate_git_capability(supports_worktree: bool) -> None:
    if not isinstance(supports_worktree, bool):
        raise TopologyDecisionError("topology_git_capability_invalid")


def _reject_json_constant(_value: str) -> NoReturn:
    raise TopologyDecisionError("topology_output_invalid_json")
