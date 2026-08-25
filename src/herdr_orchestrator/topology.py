from __future__ import annotations

import json
import re
from pathlib import Path

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
    if override is not None:
        return _validate_worktree(override, supports_worktree)
    if worker_default is not None:
        return _validate_worktree(worker_default, supports_worktree)
    if mode is not PlacementMode.HYBRID:
        return _validate_worktree(PlacementTarget(mode.value), supports_worktree)

    content = f"{title}\n{prompt}".casefold()
    if any(signal in content for signal in HARD_READ_ONLY_SIGNALS):
        return PlacementTarget.PANE
    if any(signal in content for signal in WRITE_SIGNALS):
        return PlacementTarget.WORKTREE if supports_worktree else PlacementTarget.TAB
    if any(signal in content for signal in READ_SIGNALS):
        return PlacementTarget.PANE
    return None


def topology_decision_prompt(
    title: str,
    prompt: str,
    output_file: Path,
    *,
    supports_worktree: bool,
) -> str:
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
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TopologyDecisionError("topology_output_missing") from exc
    except json.JSONDecodeError as exc:
        raise TopologyDecisionError("topology_output_invalid_json") from exc
    if not isinstance(payload, dict) or set(payload) != PLACEMENT_KEYS:
        raise TopologyDecisionError("topology_output_invalid_shape")
    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2_000:
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
    if target is PlacementTarget.WORKTREE and not supports_worktree:
        raise TopologyDecisionError("topology_worktree_requires_git")
    return target
