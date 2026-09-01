from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from herdr_orchestrator.harness_health import HarnessHealthError
from herdr_orchestrator.model import Harness, WorkflowConfig

if TYPE_CHECKING:
    from herdr_orchestrator.harness_health import EligibilitySnapshot, HarnessHealth, HealthProbe

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
        override if override is not None else (config.planner.worker_harnesses or configured)
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
    health: HarnessHealth | None = None,
    readiness_probe: HealthProbe | None = None,
    health_timeout_seconds: int | None = None,
    health_deadline: float | None = None,
    health_snapshot: EligibilitySnapshot | None = None,
) -> Harness:
    requested = None if force_auto else (override or config.planner.harness)
    if requested is not None:
        if health is not None:
            if health_snapshot is None:
                health.require(
                    requested,
                    role="controller",
                    probe=readiness_probe,
                    timeout_seconds=health_timeout_seconds,
                    deadline=health_deadline,
                    static_reason=health.static_reason(requested),
                )
            else:
                try:
                    record = health_snapshot.record_for(requested)
                except KeyError as exc:
                    raise ValueError("controller_health_snapshot_missing") from exc
                if not record.eligible_at(health_snapshot.evaluated_at):
                    health.record_selection(health_snapshot, role="controller")
                    raise HarnessHealthError("controller", requested, record)
                health.record_selection(
                    health_snapshot,
                    role="controller",
                    selected=requested,
                )
        return requested

    candidates = tuple(dict.fromkeys(worker_harnesses))
    eligible = set(candidates)
    snapshot: EligibilitySnapshot | None = None
    if health is not None:
        snapshot = health_snapshot or health.snapshot(
            candidates,
            refresh=True,
            probe=readiness_probe,
            timeout_seconds=health_timeout_seconds,
            deadline=health_deadline,
        )
        eligible = set(snapshot.eligible_harnesses)
    for harness in AUTO_CONTROLLER_ORDER:
        if harness in eligible and (
            health is not None or executable_finder(harness.value) is not None
        ):
            if health is not None and snapshot is not None:
                health.record_selection(snapshot, role="controller", selected=harness)
            return harness
    if snapshot is not None:
        assert health is not None
        health.record_selection(snapshot, role="controller")
        reasons = ",".join(
            f"{harness.value}={snapshot.record_for(harness).reason}" for harness in candidates
        )
        raise ValueError(f"controller_harness_unavailable:{reasons}")
    raise ValueError("controller_harness_unavailable")


def eligible_worker_harnesses(
    config: WorkflowConfig,
    worker_harnesses: Iterable[Harness],
    *,
    health: HarnessHealth | None = None,
    readiness_probe: HealthProbe | None = None,
    health_timeout_seconds: int | None = None,
    health_deadline: float | None = None,
    health_snapshot: EligibilitySnapshot | None = None,
) -> tuple[Harness, ...]:
    """Return the configured worker pool filtered through one health snapshot."""
    requested = tuple(dict.fromkeys(worker_harnesses))
    if health is None:
        return requested
    snapshot = health_snapshot or health.snapshot(
        requested,
        refresh=True,
        probe=readiness_probe,
        timeout_seconds=health_timeout_seconds,
        deadline=health_deadline,
    )
    health.record_selection(snapshot, role="worker")
    return tuple(harness for harness in requested if harness in snapshot.eligible_harnesses)
