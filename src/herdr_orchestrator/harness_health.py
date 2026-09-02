"""Durable, privacy-safe harness health and eligibility policy."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import select
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    HarnessHealthConfig,
    WorkflowConfig,
)
from herdr_orchestrator.observability import Observability
from herdr_orchestrator.store import Store


class HarnessHealthStatus(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


HealthStatus = HarnessHealthStatus


class HealthSource(StrEnum):
    NONE = "none"
    DOCTOR = "doctor"
    DISPATCH = "dispatch"
    PROBE = "probe"


READINESS_EXPIRED = "readiness_expired"
HEALTH_UNKNOWN = "health_unknown"
HEALTH_COOLDOWN = "health_cooldown"
_REASON = re.compile(r"[a-z][a-z0-9_.:-]{0,95}\Z")

# Task semantics and completion contracts do not describe harness health.
_TASK_FAILURES = frozenset(
    {
        "agent_blocked",
        "task_receipt_ambiguous",
        "task_receipt_invalid",
        "task_receipt_kind_invalid",
        "task_receipt_missing",
        "task_receipt_path_invalid",
        "task_receipt_stale",
        "task_receipt_unreadable",
        "completion_verification_failed",
        "completion_reported_failed",
        "completion_reported_blocked",
        "completion_envelope_missing",
        "completion_envelope_malformed",
        "completion_envelope_stale",
        "completion_envelope_duplicate",
        "completion_envelope_invalid",
        "completion_envelope_oversized",
        "completion_output_invalid",
        "completion_output_oversized",
        "completion_schema_mismatch",
        "completion_job_mismatch",
        "completion_attempt_mismatch",
        "completion_fencing_token_mismatch",
        "completion_status_invalid",
        "completion_evidence_invalid",
        "completion_evidence_oversized",
        "completion_policy_mismatch",
        "completion_policy_invalid",
        "completion_recovery_unverified",
        "task_receipt_recovery_unverified",
        "completion_result_invalid",
        "completion_identity_invalid",
    }
)
_HARD_FAILURES = frozenset(
    {
        "harness_unavailable",
        "profile_unavailable",
        "not_in_herdr",
        "herdr_unavailable",
        "agent_auth_failed",
        "agent_auth_required",
        "agent_model_invalid",
        "agent_identity_mismatch",
        "agent_not_ready",
        "agent_start_failed",
        "agent_pane_mismatch",
        "agent_workspace_mismatch",
        "herdr_pane_id_missing",
        "herdr_workspace_id_missing",
    }
)
_RETRYABLE_FAILURES = frozenset(
    {
        "herdr_timeout",
        "timeout",
        "prompt_acceptance_timeout",
        "agent_provider_failed",
        "agent_turn_not_observed",
        "herdr_invalid_response",
        "readiness_probe_failed",
        "dispatcher_unhandled_error",
        "resume_unhandled_error",
    }
)
PROBE_COMMIT_GRACE_SECONDS = 0.25


class HealthProbe(Protocol):
    def __call__(
        self,
        workflow: WorkflowConfig,
        harness: Harness,
        timeout_seconds: int,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class HarnessHealthRecord:
    workflow: str
    workspace: str
    harness: Harness
    status: HarnessHealthStatus
    reason: str
    source: str
    observed_at: float
    expires_at: float | None
    cooldown_until: float | None
    retryable_failures: int = 0
    probe_lease_until: float | None = None

    def __post_init__(self) -> None:
        if (
            not self.workflow
            or not self.workspace
            or not self.reason
            or not math.isfinite(self.observed_at)
            or any(
                value is not None and not math.isfinite(value)
                for value in (self.expires_at, self.cooldown_until, self.probe_lease_until)
            )
        ):
            raise ValueError("harness_health_record_invalid")
        if _REASON.fullmatch(self.reason) is None:
            raise ValueError("harness_health_reason_invalid")
        if self.retryable_failures < 0:
            raise ValueError("harness_health_retryable_count_invalid")

    def eligible_at(self, now: float) -> bool:
        return (
            self.status is HarnessHealthStatus.READY
            and self.expires_at is not None
            and self.expires_at > now
            and (self.cooldown_until is None or self.cooldown_until <= now)
        )

    @property
    def retryable_count(self) -> int:
        return self.retryable_failures

    def refresh_due(self, now: float) -> bool:
        if self.status is HarnessHealthStatus.UNKNOWN:
            return True
        if self.status is HarnessHealthStatus.READY:
            return self.expires_at is None or self.expires_at <= now
        return self.cooldown_until is not None and self.cooldown_until <= now

    def public_json(self, now: float) -> dict[str, object]:
        age = max(0.0, now - self.observed_at)
        return {
            "harness": self.harness.value,
            "status": self.status.value,
            "eligible": self.eligible_at(now),
            "reason": self.reason,
            "source": self.source,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "cooldown_until": self.cooldown_until,
            "age_seconds": round(age, 3),
            "retryable_failures": self.retryable_failures,
            "retryable_count": self.retryable_failures,
            "probe_lease_until": self.probe_lease_until,
        }


@dataclass(frozen=True, slots=True)
class EligibilitySnapshot:
    workflow: str
    workspace: str
    evaluated_at: float
    records: tuple[HarnessHealthRecord, ...]

    @property
    def eligible_harnesses(self) -> tuple[Harness, ...]:
        return tuple(
            record.harness for record in self.records if record.eligible_at(self.evaluated_at)
        )

    @property
    def eligible(self) -> tuple[Harness, ...]:
        return self.eligible_harnesses

    def record_for(self, harness: Harness) -> HarnessHealthRecord:
        for record in self.records:
            if record.harness is harness:
                return record
        raise KeyError(harness)

    @property
    def reasons(self) -> dict[str, str]:
        return {record.harness.value: record.reason for record in self.records}

    def public_json(self) -> dict[str, object]:
        static_reasons = {
            record.harness.value: record.reason
            for record in self.records
            if record.source == "preflight"
        }
        return {
            "workflow": self.workflow,
            "workspace_id": _workspace_id(self.workspace),
            "evaluated_at": self.evaluated_at,
            "eligible": [harness.value for harness in self.eligible_harnesses],
            "eligible_harnesses": [harness.value for harness in self.eligible_harnesses],
            "static_reasons": static_reasons,
            "excluded_reasons": self.reasons,
            "records": [record.public_json(self.evaluated_at) for record in self.records],
        }


class HarnessHealthError(ValueError):
    """Stable selection failure carrying the requested harness and evidence."""

    def __init__(
        self,
        role: str,
        harness: Harness,
        record: HarnessHealthRecord,
    ) -> None:
        self.role = role
        self.harness = harness
        self.record = record
        self.code = f"{role}_harness_unavailable"
        super().__init__(f"{self.code}:{harness.value}:{record.reason}")


class HarnessHealth:
    """Own health persistence, freshness, classification and probe deduplication."""

    def __init__(
        self,
        store: Store,
        workflow: WorkflowConfig,
        workspace: Path | None = None,
        *,
        probe: HealthProbe | None = None,
        clock: Callable[[], float | datetime] = time.time,
        observability: Observability | None = None,
        ttl_seconds: int | None = None,
        cooldown_seconds: int | None = None,
        probe_timeout_seconds: int | None = None,
        allow_live_probe: bool | None = None,
        environment: Mapping[str, str] | None = None,
        executable_finder: Callable[[str], str | None] = shutil.which,
        isolate_probes: bool | None = None,
    ) -> None:
        self.store = store
        self.workflow = workflow
        self.workflow_name = workflow.name
        self.workspace_path = (workspace or workflow.workspace).expanduser().resolve()
        self.workspace = str(self.workspace_path)
        policy = _workflow_health_policy(workflow)
        self.ttl_seconds = policy.ttl_seconds if ttl_seconds is None else ttl_seconds
        self.cooldown_seconds = (
            policy.cooldown_seconds if cooldown_seconds is None else cooldown_seconds
        )
        self.probe_timeout_seconds = (
            policy.probe_timeout_seconds if probe_timeout_seconds is None else probe_timeout_seconds
        )
        if (
            self.ttl_seconds < 1
            or self.cooldown_seconds < 1
            or not 5 <= self.probe_timeout_seconds <= 300
        ):
            raise ValueError("harness_health_policy_invalid")
        self.probe = probe
        self.clock = clock
        self.observability = observability
        self.environment = environment
        self.executable_finder = executable_finder
        self.isolate_probes = environment is not None if isolate_probes is None else isolate_probes
        self._refresh_lock = threading.Lock()
        # The ambient CI guard remains the default; tests or controlled callers
        # can explicitly opt into injected probes with ``allow_live_probe``.
        probe_environment = os.environ if environment is None else environment
        self.live_probe_allowed = (
            not any(
                _environment_flag(probe_environment.get(key)) for key in ("CI", "GITHUB_ACTIONS")
            )
            if allow_live_probe is None
            else allow_live_probe
        )

    def snapshot(
        self,
        harnesses: Iterable[Harness],
        *,
        refresh: bool = False,
        force_refresh: bool = False,
        probe: HealthProbe | None = None,
        timeout_seconds: int | None = None,
        deadline: float | None = None,
        static_reasons: Mapping[Harness, str] | None = None,
        refresh_harnesses: Iterable[Harness] | None = None,
    ) -> EligibilitySnapshot:
        if timeout_seconds is not None and timeout_seconds != 0 and not 5 <= timeout_seconds <= 300:
            raise ValueError("harness_health_probe_timeout_invalid")
        _validate_deadline(deadline)
        values = _unique_harnesses(harnesses)
        refresh_values = (
            values if refresh_harnesses is None else _unique_harnesses(refresh_harnesses)
        )
        refresh_set = set(refresh_values)
        now = self._now()
        self.store.initialize()
        effective_static_reasons = self.static_reasons(values)
        if static_reasons:
            effective_static_reasons.update(
                {harness: reason for harness, reason in static_reasons.items() if harness in values}
            )
        records = self._apply_static_reasons(
            self._records(values, now),
            effective_static_reasons,
        )
        if refresh:
            active_probe = probe or self.probe
            due = tuple(
                record.harness
                for record in records
                if record.harness in refresh_set
                and record.harness not in effective_static_reasons
                and (force_refresh or record.refresh_due(now))
            )
            if deadline is None:
                for harness in due:
                    self._refresh(harness, active_probe, timeout_seconds)
            else:
                self._refresh_concurrently(
                    due,
                    active_probe,
                    timeout_seconds,
                    deadline,
                )
            now = self._now()
            records = self._apply_static_reasons(
                self._records(values, now),
                effective_static_reasons,
            )
        return EligibilitySnapshot(self.workflow_name, self.workspace, now, records)

    def require(
        self,
        harness: Harness,
        *,
        role: str,
        probe: HealthProbe | None = None,
        timeout_seconds: int | None = None,
        deadline: float | None = None,
        static_reason: str | None = None,
    ) -> HarnessHealthRecord:
        if static_reason is not None:
            snapshot = self.snapshot(
                (harness,),
                refresh=False,
                static_reasons={harness: static_reason},
            )
            record = snapshot.record_for(harness)
            self.record_selection(
                snapshot,
                role=role,
            )
            raise HarnessHealthError(role, harness, record)
        snapshot = self.snapshot(
            (harness,),
            refresh=True,
            probe=probe,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        record = snapshot.record_for(harness)
        if not record.eligible_at(snapshot.evaluated_at):
            self.record_selection(snapshot, role=role)
            raise HarnessHealthError(role, harness, record)
        self.record_selection(snapshot, role=role, selected=harness)
        return record

    def static_reasons(self, harnesses: Iterable[Harness]) -> dict[Harness, str]:
        return {
            harness: reason
            for harness in _unique_harnesses(harnesses)
            if (reason := self.static_reason(harness)) is not None
        }

    def static_reason(self, harness: Harness) -> str | None:
        environment = self.environment
        if environment is None:
            return None

        def value(key: str) -> str:
            raw = environment.get(key, "")
            return raw if isinstance(raw, str) else ""

        if any(_environment_flag(value(key)) for key in ("CI", "GITHUB_ACTIONS")):
            return "readiness_ci_forbidden"
        if value("HERDR_ENV") != "1":
            return "not_in_herdr"
        if not value("HERDR_PANE_ID"):
            return "herdr_pane_id_missing"
        if not value("HERDR_WORKSPACE_ID"):
            return "herdr_workspace_id_missing"
        try:
            if self.executable_finder("herdr") is None:
                return "herdr_unavailable"
            if self.executable_finder(harness.value) is None:
                return "harness_unavailable"
        except Exception:
            return "harness_unavailable"
        profile = next(
            (item for item in self.workflow.profiles if item.harness is harness),
            None,
        )
        try:
            if profile is None or not profile.context_file.is_file():
                return "profile_unavailable"
        except OSError:
            return "profile_unavailable"
        return None

    def eligibility(
        self,
        harnesses: Iterable[Harness],
        *,
        refresh: bool = False,
        force_refresh: bool = False,
        probe: HealthProbe | None = None,
        timeout_seconds: int | None = None,
        deadline: float | None = None,
        static_reasons: Mapping[Harness, str] | None = None,
        refresh_harnesses: Iterable[Harness] | None = None,
    ) -> EligibilitySnapshot:
        return self.snapshot(
            harnesses,
            refresh=refresh,
            force_refresh=force_refresh,
            probe=probe,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
            static_reasons=static_reasons,
            refresh_harnesses=refresh_harnesses,
        )

    def record_selection(
        self,
        snapshot: EligibilitySnapshot,
        *,
        role: str,
        selected: Harness | None = None,
    ) -> None:
        if self.observability is None:
            return
        self.observability.event(
            "harness_selection",
            correlation_id="",
            fields={
                "role": role,
                "workspace_id": _workspace_id(snapshot.workspace),
                "selected": None if selected is None else selected.value,
                "eligible": [harness.value for harness in snapshot.eligible_harnesses],
                "excluded_reasons": snapshot.reasons,
            },
        )

    def run_probe(
        self,
        harness: Harness,
        probe: HealthProbe,
        *,
        timeout_seconds: int | None = None,
        source: str | HealthSource = HealthSource.DOCTOR,
        force: bool = True,
        deadline: float | None = None,
    ) -> Mapping[str, object]:
        """Run one leased probe, including a forced doctor refresh, and return its raw shape."""
        self.store.initialize()
        if not self.live_probe_allowed:
            result = {
                "status": HarnessHealthStatus.UNAVAILABLE.value,
                "error_code": "readiness_ci_forbidden",
            }
            self.record_probe(harness, result, source=source)
            return result
        now = self._now()
        current = self._records((harness,), now)[0]
        if not force and not current.refresh_due(now):
            return {"status": current.status.value, "error_code": current.reason}
        return self._probe_with_lease(
            harness,
            probe,
            timeout_seconds,
            source=source,
            deadline=deadline,
            force=force,
        )

    def record_probe(
        self,
        harness: Harness,
        result: Mapping[str, object] | object,
        *,
        source: str | HealthSource = HealthSource.DOCTOR,
        observed_at: float | None = None,
        expected_revision: int | None = None,
        expected_owner: str | None = None,
    ) -> HarnessHealthRecord:
        self.store.initialize()
        now = self._now() if observed_at is None else _coerce_time(observed_at)
        status, reason = _classify_probe(result)
        previous = self._records((harness,), now)[0]
        failures = previous.retryable_failures + 1 if status is HarnessHealthStatus.DEGRADED else 0
        cooldown_until = (
            now + self.cooldown_seconds
            if status in {HarnessHealthStatus.DEGRADED, HarnessHealthStatus.UNAVAILABLE}
            else None
        )
        expires_at = now + self.ttl_seconds if status is HarnessHealthStatus.READY else None
        record = HarnessHealthRecord(
            self.workflow_name,
            self.workspace,
            harness,
            status,
            reason,
            _source_value(source),
            now,
            expires_at,
            cooldown_until,
            failures,
        )
        written = self._write(
            record,
            expected_revision=expected_revision,
            expected_owner=expected_owner,
            clear_probe_lease=expected_revision is not None,
        )
        if not written:
            return self._records((harness,), self._now())[0]
        self._event("harness_health_changed", record, now)
        return record

    def record_dispatch(
        self,
        harness: Harness,
        outcome: DispatchOutcome,
        *,
        observed_at: float | None = None,
    ) -> HarnessHealthRecord | None:
        """Classify runtime outcome; task-specific blocked/receipt failures are neutral."""
        code = _bounded_reason(outcome.error_code)
        if (
            outcome.state is AgentState.BLOCKED
            and code not in _HARD_FAILURES
            and code not in _RETRYABLE_FAILURES
            and code not in _TASK_FAILURES
        ):
            return None
        if code in _TASK_FAILURES:
            if outcome.agent_settled is not True and outcome.state not in {
                AgentState.IDLE,
                AgentState.DONE,
            }:
                return None
            return self.record_probe(
                harness,
                {"status": HarnessHealthStatus.READY.value},
                source=HealthSource.DISPATCH,
                observed_at=observed_at,
            )
        settled = outcome.agent_settled is True or outcome.state in {
            AgentState.IDLE,
            AgentState.DONE,
        }
        if settled and not code:
            result: Mapping[str, object] = {"status": HarnessHealthStatus.READY.value}
        elif code in _HARD_FAILURES:
            result = {"status": HarnessHealthStatus.UNAVAILABLE.value, "error_code": code}
        elif code in _RETRYABLE_FAILURES or not settled:
            result = {"status": HarnessHealthStatus.DEGRADED.value, "error_code": code}
        else:
            result = {"status": HarnessHealthStatus.DEGRADED.value, "error_code": code}
        return self.record_probe(
            harness,
            result,
            source=HealthSource.DISPATCH,
            observed_at=observed_at,
        )

    def _records(
        self,
        harnesses: tuple[Harness, ...],
        now: float,
    ) -> tuple[HarnessHealthRecord, ...]:
        rows = self.store.harness_health_rows(
            self.workflow_name,
            self.workspace,
            harnesses=harnesses,
        )
        by_harness = {Harness(str(row["harness"])): _record_from_row(row) for row in rows}
        records: list[HarnessHealthRecord] = []
        for harness in harnesses:
            record = by_harness.get(harness)
            if record is None:
                record = HarnessHealthRecord(
                    self.workflow_name,
                    self.workspace,
                    harness,
                    HarnessHealthStatus.UNKNOWN,
                    HEALTH_UNKNOWN,
                    HealthSource.NONE.value,
                    now,
                    None,
                    None,
                    0,
                )
                self.store.ensure_harness_health(
                    workflow=self.workflow_name,
                    workspace=self.workspace,
                    harness=harness,
                    observed_at=now,
                )
            elif record.status is HarnessHealthStatus.READY and not record.eligible_at(now):
                record = replace(
                    record,
                    status=HarnessHealthStatus.UNKNOWN,
                    reason=READINESS_EXPIRED,
                )
            records.append(record)
        return tuple(records)

    @staticmethod
    def _apply_static_reasons(
        records: tuple[HarnessHealthRecord, ...],
        static_reasons: Mapping[Harness, str] | None,
    ) -> tuple[HarnessHealthRecord, ...]:
        if not static_reasons:
            return records
        return tuple(
            (
                replace(
                    record,
                    status=HarnessHealthStatus.UNAVAILABLE,
                    reason=reason,
                    source="preflight",
                    expires_at=None,
                    cooldown_until=None,
                )
                if (reason := static_reasons.get(record.harness)) is not None
                else record
            )
            for record in records
        )

    def _refresh(
        self,
        harness: Harness,
        probe: HealthProbe | None,
        timeout_seconds: int | None,
        *,
        deadline: float | None = None,
    ) -> None:
        if probe is None or timeout_seconds == 0 or not self.live_probe_allowed:
            return
        self._probe_with_lease(
            harness,
            probe,
            timeout_seconds,
            source=HealthSource.PROBE,
            deadline=deadline,
        )

    def _refresh_concurrently(
        self,
        harnesses: tuple[Harness, ...],
        probe: HealthProbe | None,
        timeout_seconds: int | None,
        deadline: float,
    ) -> None:
        if not harnesses or probe is None or timeout_seconds == 0 or not self.live_probe_allowed:
            return
        runs: list[threading.Thread] = []
        for harness in harnesses:
            if deadline - time.monotonic() <= 0:
                break
            thread = threading.Thread(
                target=self._refresh,
                args=(harness, probe, timeout_seconds),
                kwargs={"deadline": deadline},
                daemon=False,
                name=f"harness-health-{harness.value}",
            )
            thread.start()
            runs.append(thread)
        for thread in runs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def _probe_with_lease(
        self,
        harness: Harness,
        probe: HealthProbe,
        timeout_seconds: int | None,
        *,
        source: str | HealthSource,
        deadline: float | None,
        force: bool = False,
    ) -> Mapping[str, object]:
        if not self.live_probe_allowed:
            return {"status": "unavailable", "error_code": "readiness_ci_forbidden"}
        now = self._now()
        configured_timeout = timeout_seconds or self.probe_timeout_seconds
        deadline_exhausted = False
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= PROBE_COMMIT_GRACE_SECONDS:
                deadline_exhausted = True
            else:
                configured_timeout = min(configured_timeout, max(1, int(remaining)))
        probe_deadline = deadline or (time.monotonic() + configured_timeout)
        owner = f"health-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:12]}"
        lease_seconds = max(30.0, float(timeout_seconds or self.probe_timeout_seconds) + 5.0)
        with self._refresh_lock:
            lease = self.store.acquire_harness_probe_lease(
                workflow=self.workflow_name,
                workspace=self.workspace,
                harness=harness,
                owner=owner,
                now=now,
                lease_seconds=lease_seconds,
                force=force,
            )
        if lease is None:
            return {"status": "error", "error_code": "readiness_probe_in_progress"}
        try:
            result: Mapping[str, object]
            if deadline_exhausted:
                result = {
                    "status": "timeout",
                    "error_code": "timeout",
                }
            else:
                try:
                    result = self._invoke_probe(
                        probe,
                        harness,
                        configured_timeout,
                        deadline=probe_deadline,
                    )
                except Exception:
                    result = {
                        "status": HarnessHealthStatus.DEGRADED.value,
                        "error_code": "readiness_probe_failed",
                    }
            if probe_deadline - time.monotonic() <= 0:
                result = {
                    "status": "timeout",
                    "error_code": "timeout",
                }
            result = _normalize_probe_result(result)
            observed_at = self._now()
            persisted = self.record_probe(
                harness,
                result,
                source=source,
                observed_at=observed_at,
                expected_revision=lease.revision,
                expected_owner=owner,
            )
            expected_status, expected_reason = _classify_probe(result)
            if (
                persisted.status is expected_status
                and persisted.reason == expected_reason
                and persisted.source == _source_value(source)
                and persisted.observed_at == observed_at
            ):
                return result
            return persisted.public_json(self._now())
        finally:
            with suppress(Exception):
                self.store.release_harness_probe(
                    workflow=self.workflow_name,
                    workspace=self.workspace,
                    harness=harness,
                    owner=owner,
                )

    def _invoke_probe(
        self,
        probe: HealthProbe,
        harness: Harness,
        timeout_seconds: int,
        *,
        deadline: float | None,
    ) -> Mapping[str, object]:
        if deadline is None:
            return _normalize_probe_result(probe(self.workflow, harness, timeout_seconds))
        if self.isolate_probes and hasattr(os, "fork"):
            return self._invoke_probe_subprocess(
                probe,
                harness,
                timeout_seconds,
                deadline,
            )
        return self._invoke_probe_thread(
            probe,
            harness,
            timeout_seconds,
            deadline,
        )

    def _invoke_probe_thread(
        self,
        probe: HealthProbe,
        harness: Harness,
        timeout_seconds: int,
        deadline: float,
    ) -> Mapping[str, object]:
        result: dict[str, object] = {}
        error: list[Exception] = []
        finished = threading.Event()

        def invoke() -> None:
            try:
                candidate = probe(self.workflow, harness, timeout_seconds)
                normalized = _normalize_probe_result(candidate)
                result.update(normalized)
            except Exception as exc:
                error.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=invoke, daemon=True, name="harness-health-probe")
        thread.start()
        remaining = max(0.0, deadline - PROBE_COMMIT_GRACE_SECONDS - time.monotonic())
        if not finished.wait(timeout=remaining):
            return {"status": "timeout", "error_code": "timeout"}
        if error:
            raise error[0]
        return result

    def _invoke_probe_subprocess(
        self,
        probe: HealthProbe,
        harness: Harness,
        timeout_seconds: int,
        deadline: float,
    ) -> Mapping[str, object]:
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                payload = json.dumps(
                    ["ok", _normalize_probe_result(probe(self.workflow, harness, timeout_seconds))]
                ).encode()
            except BaseException:
                payload = json.dumps(["error", "readiness_probe_failed"]).encode()
            try:
                os.write(write_fd, payload)
            finally:
                os.close(write_fd)
                os._exit(0)

        os.close(write_fd)
        data = bytearray()
        try:
            while True:
                remaining = deadline - PROBE_COMMIT_GRACE_SECONDS - time.monotonic()
                if remaining <= 0:
                    _terminate_probe_process(child)
                    return {"status": "timeout", "error_code": "timeout"}
                ready, _, _ = select.select([read_fd], [], [], remaining)
                if not ready:
                    _terminate_probe_process(child)
                    return {"status": "timeout", "error_code": "timeout"}
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                data.extend(chunk)
        finally:
            os.close(read_fd)
        _, status = os.waitpid(child, 0)
        if not data:
            return {"status": "error", "error_code": "readiness_probe_failed"}
        try:
            decoded = json.loads(bytes(data))
            if not isinstance(decoded, list) or len(decoded) != 2:
                raise ValueError("readiness_result_invalid")
            result_kind, payload = decoded
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return {"status": "error", "error_code": "readiness_result_invalid"}
        del status
        if result_kind != "ok":
            return {"status": "error", "error_code": "readiness_probe_failed"}
        return _normalize_probe_result(payload)

    def _write(
        self,
        record: HarnessHealthRecord,
        *,
        expected_revision: int | None = None,
        expected_owner: str | None = None,
        clear_probe_lease: bool = False,
    ) -> bool:
        return self.store.upsert_harness_health(
            workflow=record.workflow,
            workspace=record.workspace,
            harness=record.harness,
            status=record.status.value,
            reason=record.reason,
            source=record.source,
            observed_at=record.observed_at,
            expires_at=record.expires_at,
            cooldown_until=record.cooldown_until,
            retryable_failures=record.retryable_failures,
            probe_lease_until=record.probe_lease_until,
            expected_revision=expected_revision,
            expected_owner=expected_owner,
            clear_probe_lease=clear_probe_lease,
        )

    def _now(self) -> float:
        return _coerce_time(self.clock())

    def _event(self, name: str, record: HarnessHealthRecord, now: float) -> None:
        if self.observability is None:
            return
        self.observability.event(
            name,
            correlation_id="",
            fields={
                "harness": record.harness.value,
                "workspace_id": _workspace_id(record.workspace),
                "status": record.status.value,
                "reason": record.reason,
                "source": record.source,
                "eligible": record.eligible_at(now),
                "age_seconds": max(0.0, now - record.observed_at),
                "retryable_failures": record.retryable_failures,
            },
        )


def _unique_harnesses(harnesses: Iterable[Harness]) -> tuple[Harness, ...]:
    values = tuple(harnesses)
    if any(not isinstance(harness, Harness) for harness in values):
        raise ValueError("harness_health_harness_invalid")
    return tuple(dict.fromkeys(values))


def _validate_deadline(deadline: float | None) -> None:
    if deadline is None:
        return
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise ValueError("harness_health_deadline_invalid")
    if not math.isfinite(float(deadline)):
        raise ValueError("harness_health_deadline_invalid")


def _environment_flag(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}


def _normalize_probe_result(result: Mapping[str, object] | object) -> Mapping[str, object]:
    if isinstance(result, Mapping):
        return result
    public_json = getattr(result, "public_json", None)
    if callable(public_json):
        candidate = public_json()
        if isinstance(candidate, Mapping):
            return candidate
    return {
        "status": HarnessHealthStatus.DEGRADED.value,
        "error_code": "readiness_result_invalid",
    }


def _terminate_probe_process(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    with suppress(ChildProcessError):
        os.waitpid(pid, 0)


def _workspace_id(workspace: str) -> str:
    return hashlib.sha256(workspace.encode()).hexdigest()[:16]


def _workflow_health_policy(workflow: WorkflowConfig) -> HarnessHealthConfig:
    policy = workflow.harness_health
    defaults = HarnessHealthConfig()
    coordinator = workflow.coordinator
    if policy == defaults and (
        coordinator.readiness_ttl_seconds != defaults.ttl_seconds
        or coordinator.readiness_cooldown_seconds != defaults.cooldown_seconds
        or coordinator.readiness_probe_timeout_seconds != defaults.probe_timeout_seconds
    ):
        return HarnessHealthConfig(
            ttl_seconds=coordinator.readiness_ttl_seconds,
            cooldown_seconds=coordinator.readiness_cooldown_seconds,
            probe_timeout_seconds=coordinator.readiness_probe_timeout_seconds,
        )
    return policy


def _record_from_row(row: Mapping[str, object]) -> HarnessHealthRecord:
    try:
        status = HarnessHealthStatus(str(row["status"]))
        harness = Harness(str(row["harness"]))
    except ValueError as exc:
        raise ValueError("harness_health_row_invalid") from exc
    observed_at = _required_float(row.get("observed_at"))
    expires_at = _optional_float(row.get("expires_at"))
    cooldown_until = _optional_float(row.get("cooldown_until"))
    if not math.isfinite(observed_at) or any(
        value is not None and not math.isfinite(value) for value in (expires_at, cooldown_until)
    ):
        raise ValueError("harness_health_time_invalid")
    return HarnessHealthRecord(
        str(row["workflow"]),
        str(row["workspace"]),
        harness,
        status,
        _bounded_reason(str(row["reason"])) or HEALTH_UNKNOWN,
        _bounded_reason(str(row["source"])) or HealthSource.NONE.value,
        observed_at,
        expires_at,
        cooldown_until,
        _required_int(row.get("retryable_failures", 0)),
        _optional_float(row.get("probe_lease_until")),
    )


def _probe_mapping(result: Mapping[str, object] | object) -> Mapping[str, object] | None:
    public_json = getattr(result, "public_json", None)
    if not isinstance(result, Mapping) and callable(public_json):
        result = public_json()
    return result if isinstance(result, Mapping) else None


def _classify_probe_status(status: str, reason: str) -> tuple[HarnessHealthStatus, str]:
    if status == "expired" or reason == "readiness_evidence_expired":
        return HarnessHealthStatus.UNKNOWN, READINESS_EXPIRED
    if status == HarnessHealthStatus.READY.value and not reason:
        return HarnessHealthStatus.READY, "readiness_ready"
    if reason in _RETRYABLE_FAILURES:
        return HarnessHealthStatus.DEGRADED, reason
    if reason in _HARD_FAILURES or status in {"auth_required", "model_invalid", "unavailable"}:
        return HarnessHealthStatus.UNAVAILABLE, reason or f"readiness_{status or 'unavailable'}"
    if status in {"timeout", "error", "degraded"}:
        return HarnessHealthStatus.DEGRADED, reason or f"readiness_{status or 'error'}"
    return HarnessHealthStatus.DEGRADED, reason or "readiness_result_invalid"


def _classify_probe(result: Mapping[str, object] | object) -> tuple[HarnessHealthStatus, str]:
    payload = _probe_mapping(result)
    if payload is None:
        return HarnessHealthStatus.DEGRADED, "readiness_result_invalid"
    raw_status = payload.get("status")
    status = str(raw_status.value if isinstance(raw_status, StrEnum) else raw_status or "")
    raw_reason = payload.get("error_code")
    reason = _bounded_reason(raw_reason)
    if raw_reason not in (None, "") and not reason:
        return HarnessHealthStatus.DEGRADED, "readiness_reason_invalid"
    return _classify_probe_status(status, reason)


def _bounded_reason(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    return text[:96] if _REASON.fullmatch(text[:96]) else ""


def _source_value(source: str | HealthSource) -> str:
    value = source.value if isinstance(source, HealthSource) else str(source).strip().lower()
    if not value or _REASON.fullmatch(value) is None:
        raise ValueError("harness_health_source_invalid")
    return value[:96]


def _coerce_time(value: object) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("harness_health_clock_timezone_missing")
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("harness_health_clock_invalid")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("harness_health_clock_invalid")
    return converted


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("harness_health_float_invalid")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("harness_health_float_invalid")
    return converted


def _required_float(value: object) -> float:
    converted = _optional_float(value)
    if converted is None:
        raise ValueError("harness_health_float_missing")
    return converted


def _required_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("harness_health_integer_invalid")
    return int(value)


__all__ = [
    "EligibilitySnapshot",
    "HarnessHealth",
    "HarnessHealthError",
    "HarnessHealthRecord",
    "HarnessHealthStatus",
    "HealthStatus",
    "HealthProbe",
    "HealthSource",
    "HEALTH_COOLDOWN",
    "HEALTH_UNKNOWN",
    "READINESS_EXPIRED",
]
