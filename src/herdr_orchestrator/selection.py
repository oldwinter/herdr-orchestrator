from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable

from herdr_orchestrator.model import Harness, WorkflowConfig

ExecutableFinder = Callable[[str], str | None]

AUTO_CONTROLLER_ORDER = (
    Harness.DROID,
    Harness.GROK,
    Harness.CODEX,
    Harness.CLAUDE,
    Harness.HERMES,
    Harness.PI,
)


def effective_worker_harnesses(
    config: WorkflowConfig,
    override: Iterable[Harness] | None = None,
) -> tuple[Harness, ...]:
    configured = tuple(worker.harness for worker in config.workers)
    requested = tuple(
        override
        if override is not None
        else (config.planner.worker_harnesses or configured)
    )
    if not requested:
        raise ValueError("worker_harnesses_empty")
    if len(set(requested)) != len(requested):
        raise ValueError("worker_harness_duplicate")
    configured_set = set(configured)
    if any(harness not in configured_set for harness in requested):
        raise ValueError("worker_harness_has_no_worker")
    return requested


def select_controller_harness(
    config: WorkflowConfig,
    *,
    worker_harnesses: Iterable[Harness],
    override: Harness | None = None,
    force_auto: bool = False,
    executable_finder: ExecutableFinder = shutil.which,
) -> Harness:
    requested = None if force_auto else (override or config.planner.harness)
    if requested is not None:
        return requested

    candidates = set(worker_harnesses)
    for harness in AUTO_CONTROLLER_ORDER:
        if harness in candidates and executable_finder(harness.value) is not None:
            return harness
    raise ValueError("controller_harness_unavailable")
