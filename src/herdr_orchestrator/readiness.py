"""Durable, privacy-safe harness readiness classification and eligibility."""

from __future__ import annotations

import secrets
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.herdr import HerdrTransport, doctor_agent_name
from herdr_orchestrator.model import (
    AgentState,
    DispatchContext,
    DispatchOutcome,
    Harness,
    HarnessHealthStatus,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
    WorkflowConfig,
)
from herdr_orchestrator.store import Store

ReadinessProbe = Callable[[WorkflowConfig, Harness, int], Mapping[str, object]]
ExecutableFinder = Callable[[str], str | None]


class ReadinessObservability(Protocol):
    def event(
        self,
        name: str,
        *,
        correlation_id: str,
        fields: Mapping[str, object] | None = None,
    ) -> None: ...

    def metric(
        self,
        name: str,
        value: float,
        *,
        correlation_id: str,
        fields: Mapping[str, object] | None = None,
    ) -> None: ...


HARD_FAILURES = {
    "agent_auth_failed",
    "agent_auth_required",
    "agent_model_invalid",
    "harness_unavailable",
    "herdr_unavailable",
    "not_in_herdr",
    "profile_unavailable",
}
TASK_LEVEL_ERRORS = {
    "agent_blocked",
    "task_receipt_ambiguous",
    "task_receipt_invalid",
    "task_receipt_missing",
    "task_receipt_stale",
}


def probe_harness_readiness(
    workflow: WorkflowConfig,
    harness: Harness,
    timeout_seconds: int,
    *,
    transport: HerdrTransport | None = None,
) -> Mapping[str, object]:
    active_transport = transport or HerdrTransport(workflow.name, workflow.workspace)
    name = doctor_agent_name(workflow.name, harness)
    prefix = f"HERDR-DOCTOR-OK harness={harness.value}"
    started = time.monotonic()
    try:
        outcome = active_transport.dispatch(
            harness,
            (
                "Read-only readiness probe. Do not modify files or external state. "
                f"Reply with exactly this line: {prefix}"
            ),
            timeout_seconds=timeout_seconds,
            agent_name=name,
            context=DispatchContext(
                placement=PlacementTarget.TAB,
                title=f"doctor-{harness.value}",
                task_key=f"doctor-{harness.value}",
                receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, prefix),
            ),
        )
    finally:
        active_transport.close_created_agent(name)
    status_by_error = {
        "agent_auth_failed": "auth_required",
        "agent_auth_required": "auth_required",
        "agent_model_invalid": "model_invalid",
        "herdr_timeout": "timeout",
        "timeout": "timeout",
        "prompt_acceptance_timeout": "timeout",
        "agent_provider_failed": "error",
        "herdr_unavailable": "unavailable",
        "not_in_herdr": "unavailable",
    }
    if outcome.state in {AgentState.IDLE, AgentState.DONE} and outcome.task_verified is True:
        status = "ready"
    else:
        status = status_by_error.get(outcome.error_code or "", "error")
    return {
        "status": status,
        "error_code": outcome.error_code,
        "error_summary": outcome.error_summary,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        "phase_timings_ms": outcome.phase_timings_ms or {},
    }


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("harness_health_number_invalid")
    return float(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("harness_health_integer_invalid")
    return value


@dataclass(frozen=True, slots=True)
class HarnessHealth:
    harness: Harness
    status: HarnessHealthStatus
    reason_code: str | None
    source: str
    observed_at: float
    expires_at: float
    cooldown_until: float
    consecutive_failures: int
    probe_lease_until: float | None = None

    def eligible_at(self, now: float) -> bool:
        return self.status is HarnessHealthStatus.READY and self.expires_at > now

    def refreshable_at(self, now: float) -> bool:
        return self.expires_at <= now and self.cooldown_until <= now


class HarnessHealthRegistry:
    """Provide one readiness interface to selection, doctor, and dispatch."""

    def __init__(
        self,
        config: WorkflowConfig,
        store: Store,
        *,
        probe: ReadinessProbe | None = None,
        executable_finder: ExecutableFinder = shutil.which,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        observability: ReadinessObservability | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.probe = probe
        self.executable_finder = executable_finder
        self.clock = clock
        self.monotonic = monotonic
        self.observability = observability
        self.workspace = str(config.workspace.resolve())

    def eligible(
        self,
        harnesses: Iterable[Harness],
        *,
        refresh: bool = True,
        deadline: float | None = None,
    ) -> tuple[Harness, ...]:
        candidates = tuple(dict.fromkeys(harnesses))
        if not candidates:
            return ()
        self.store.initialize()
        now = self.clock()
        records = self._records(candidates)
        if refresh:
            for harness in candidates:
                record = records.get(harness)
                if record is not None and record.eligible_at(now):
                    continue
                if record is not None and not record.refreshable_at(now):
                    continue
                timeout_seconds = self._probe_timeout(deadline)
                if timeout_seconds is None:
                    break
                self._refresh(harness, now, timeout_seconds)
            records = self._records(candidates)
        return tuple(
            harness
            for harness in candidates
            if (record := records.get(harness)) is not None and record.eligible_at(now)
        )

    def require(
        self,
        harness: Harness,
        *,
        deadline: float | None = None,
    ) -> Harness:
        if harness in self.eligible((harness,), deadline=deadline):
            return harness
        reason = self.reason(harness) or "readiness_unknown"
        raise ValueError(f"harness_unavailable:{harness.value}:{reason}")

    def reason(self, harness: Harness) -> str | None:
        record = self._records((harness,)).get(harness)
        return record.reason_code if record is not None else "readiness_unknown"

    def record_probe(
        self,
        harness: Harness,
        result: Mapping[str, object],
        *,
        source: str = "doctor",
        probe_token: str | None = None,
    ) -> None:
        status_value = str(result.get("status", "error"))
        error_code_value = result.get("error_code")
        error_code = str(error_code_value) if error_code_value else None
        if status_value == "ready":
            status = HarnessHealthStatus.READY
            reason_code = None
        elif error_code in HARD_FAILURES or status_value in {
            "auth_required",
            "model_invalid",
            "unavailable",
        }:
            status = HarnessHealthStatus.UNAVAILABLE
            reason_code = error_code or f"readiness_{status_value}"
        else:
            status = HarnessHealthStatus.DEGRADED
            reason_code = error_code or "readiness_probe_failed"
        self._record(
            harness,
            status,
            reason_code,
            source,
            probe_token=probe_token,
        )

    def record_dispatch(
        self,
        harness: Harness,
        outcome: DispatchOutcome,
        *,
        workspace: Path | None = None,
    ) -> None:
        if outcome.state is AgentState.BLOCKED and (
            outcome.error_code is None or outcome.error_code in TASK_LEVEL_ERRORS
        ):
            return
        if outcome.state in {AgentState.IDLE, AgentState.DONE} and (
            outcome.agent_settled is not False
            and (outcome.error_code is None or outcome.error_code in TASK_LEVEL_ERRORS)
        ):
            self._record(
                harness,
                HarnessHealthStatus.READY,
                None,
                "dispatch",
                workspace=workspace,
            )
        elif outcome.error_code in HARD_FAILURES:
            self._record(
                harness,
                HarnessHealthStatus.UNAVAILABLE,
                outcome.error_code,
                "dispatch",
                workspace=workspace,
            )
        else:
            self._record(
                harness,
                HarnessHealthStatus.DEGRADED,
                outcome.error_code or "dispatch_runtime_failed",
                "dispatch",
                workspace=workspace,
            )

    def projection(
        self,
        harnesses: Iterable[Harness],
        *,
        workspace: Path | None = None,
    ) -> list[dict[str, object]]:
        candidates = tuple(dict.fromkeys(harnesses))
        self.store.initialize()
        now = self.clock()
        records = self._records(candidates, workspace=workspace)
        result: list[dict[str, object]] = []
        for harness in candidates:
            record = records.get(harness)
            expired = (
                record is not None
                and record.status is HarnessHealthStatus.READY
                and not record.eligible_at(now)
            )
            result.append(
                {
                    "harness": harness.value,
                    "status": (
                        HarnessHealthStatus.UNKNOWN.value
                        if record is None or expired
                        else record.status.value
                    ),
                    "eligible": record.eligible_at(now) if record is not None else False,
                    "reason_code": (
                        "readiness_expired"
                        if expired
                        else (record.reason_code if record is not None else "readiness_unknown")
                    ),
                    "source": record.source if record is not None else "none",
                    "age_seconds": (
                        max(0, int(now - record.observed_at))
                        if record is not None and record.observed_at > 0
                        else None
                    ),
                    "expires_at": record.expires_at if record is not None else None,
                    "cooldown_until": record.cooldown_until if record is not None else None,
                    "consecutive_failures": (
                        record.consecutive_failures if record is not None else 0
                    ),
                }
            )
        return result

    def _probe_timeout(self, deadline: float | None) -> int | None:
        configured = self.config.coordinator.readiness_probe_timeout_seconds
        if deadline is None:
            return configured
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            return None
        return max(1, min(configured, ceil(remaining)))

    def _refresh(self, harness: Harness, now: float, timeout_seconds: int) -> None:
        if self.executable_finder(harness.value) is None:
            self._record(
                harness,
                HarnessHealthStatus.UNAVAILABLE,
                "harness_unavailable",
                "preflight",
            )
            return
        probe_token = secrets.token_hex(16)
        if not self.store.claim_harness_probe(
            self.config.name,
            self.workspace,
            harness,
            now=now,
            lease_seconds=timeout_seconds + 10,
            probe_token=probe_token,
        ):
            return
        try:
            result = self._run_probe(harness, timeout_seconds)
            self.record_probe(
                harness,
                result,
                source="preflight",
                probe_token=probe_token,
            )
        except Exception:
            self._record(
                harness,
                HarnessHealthStatus.DEGRADED,
                "readiness_probe_failed",
                "preflight",
                probe_token=probe_token,
            )
        finally:
            self.store.release_harness_probe(
                self.config.name,
                self.workspace,
                harness,
                probe_token=probe_token,
            )

    def _run_probe(
        self,
        harness: Harness,
        timeout_seconds: int,
    ) -> Mapping[str, object]:
        if self.probe is not None:
            return self.probe(
                self.config,
                harness,
                timeout_seconds,
            )
        return probe_harness_readiness(
            self.config,
            harness,
            timeout_seconds,
        )

    def _record(
        self,
        harness: Harness,
        status: HarnessHealthStatus,
        reason_code: str | None,
        source: str,
        *,
        workspace: Path | None = None,
        probe_token: str | None = None,
    ) -> None:
        self.store.initialize()
        now = self.clock()
        workspace_key = self._workspace_key(workspace)
        existing = self._records((harness,), workspace=workspace).get(harness)
        failures = (
            0
            if status is HarnessHealthStatus.READY
            else (existing.consecutive_failures if existing is not None else 0) + 1
        )
        cooldown_until = (
            now
            if status is HarnessHealthStatus.READY
            else now + self.config.coordinator.readiness_cooldown_seconds
        )
        recorded = self.store.record_harness_health(
            self.config.name,
            workspace_key,
            harness,
            status=status,
            reason_code=reason_code,
            source=source,
            observed_at=now,
            expires_at=(
                now + self.config.coordinator.readiness_ttl_seconds
                if status is HarnessHealthStatus.READY
                else now
            ),
            cooldown_until=cooldown_until,
            consecutive_failures=failures,
            probe_token=probe_token,
        )
        if not recorded:
            return
        if self.observability is not None:
            correlation_id = f"harness-health-{harness.value}"
            fields: dict[str, object] = {
                "consecutive_failures": failures,
                "harness": harness.value,
                "reason_code": reason_code,
                "source": source,
                "status": status.value,
            }
            with suppress(Exception):
                self.observability.event(
                    "harness_health_observed",
                    correlation_id=correlation_id,
                    fields=fields,
                )
                self.observability.metric(
                    "harness_readiness_eligible",
                    float(status is HarnessHealthStatus.READY),
                    correlation_id=correlation_id,
                    fields=fields,
                )
                if existing is None or (
                    existing.status is not status or existing.reason_code != reason_code
                ):
                    self.observability.event(
                        "harness_health_transition",
                        correlation_id=correlation_id,
                        fields={
                            **fields,
                            "previous_reason_code": (
                                existing.reason_code if existing is not None else None
                            ),
                            "previous_status": (
                                existing.status.value
                                if existing is not None
                                else HarnessHealthStatus.UNKNOWN.value
                            ),
                        },
                    )

    def _workspace_key(self, workspace: Path | None) -> str:
        return self.workspace if workspace is None else str(workspace.resolve())

    def _records(
        self,
        harnesses: Iterable[Harness],
        *,
        workspace: Path | None = None,
    ) -> dict[Harness, HarnessHealth]:
        rows = self.store.harness_health(
            self.config.name,
            self._workspace_key(workspace),
            harnesses,
        )
        return {
            harness: HarnessHealth(
                harness=harness,
                status=HarnessHealthStatus(str(row["status"])),
                reason_code=(str(row["reason_code"]) if row["reason_code"] is not None else None),
                source=str(row["source"]),
                observed_at=_number(row["observed_at"]),
                expires_at=_number(row["expires_at"]),
                cooldown_until=_number(row["cooldown_until"]),
                consecutive_failures=_integer(row["consecutive_failures"]),
                probe_lease_until=(
                    _number(row["probe_lease_until"])
                    if row["probe_lease_until"] is not None
                    else None
                ),
            )
            for harness, row in rows.items()
        }
