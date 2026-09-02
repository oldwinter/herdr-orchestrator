"""Stable standardized-delivery run identity and legacy-root recovery."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from herdr_orchestrator.model import Harness, WorkflowConfig
from herdr_orchestrator.selection import AUTO_CONTROLLER_ORDER


def delivery_run_id(
    config: WorkflowConfig,
    goal: str,
    *,
    controller_request: str,
    worker_harnesses: Iterable[Harness],
) -> str:
    delivery_config = config.standardized_delivery
    identity = (
        f"{config.name}\0{config.workspace.resolve()}\0{goal}\0"
        f"{delivery_config.tracker_backend.value}\0"
        f"{delivery_config.tracker_root}\0"
        f"{delivery_config.github_repository}\0"
        f"{delivery_config.wayfinder.value}\0"
        f"{delivery_config.max_parallel}\0"
        f"{delivery_config.review_repair_rounds}\0"
        f"controller-request={controller_request}\0"
        f"workers={','.join(harness.value for harness in worker_harnesses)}"
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:12]


def legacy_delivery_run_id(
    config: WorkflowConfig,
    goal: str,
    *,
    controller: Harness,
    worker_harnesses: Iterable[Harness],
) -> str:
    delivery_config = config.standardized_delivery
    identity = (
        f"{config.name}\0{config.workspace.resolve()}\0{goal}\0"
        f"{delivery_config.tracker_backend.value}\0"
        f"{delivery_config.tracker_root}\0"
        f"{delivery_config.github_repository}\0"
        f"{delivery_config.wayfinder.value}\0"
        f"{delivery_config.max_parallel}\0"
        f"{delivery_config.review_repair_rounds}\0"
        f"{controller.value}\0"
        f"{','.join(harness.value for harness in worker_harnesses)}"
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:12]


def recoverable_delivery_identity(
    config: WorkflowConfig,
    goal: str,
    run_id: str,
    selected_controller: Harness,
    worker_harnesses: Iterable[Harness],
) -> tuple[str, Path]:
    artifact_root = config.standardized_delivery.artifact_root
    current_root = artifact_root / run_id
    if delivery_root_has_state(current_root):
        return run_id, current_root
    candidates = [selected_controller]
    candidates.extend(
        harness
        for harness in AUTO_CONTROLLER_ORDER
        if harness in worker_harnesses and harness not in candidates
    )
    for controller in candidates:
        legacy_id = legacy_delivery_run_id(
            config,
            goal,
            controller=controller,
            worker_harnesses=worker_harnesses,
        )
        legacy_root = artifact_root / legacy_id
        if legacy_id != run_id and delivery_root_has_state(legacy_root):
            return legacy_id, legacy_root
    return run_id, current_root


def delivery_root_has_state(root: Path) -> bool:
    return any((root / name).is_file() for name in ("state.json", "journal.jsonl", "result.json"))
