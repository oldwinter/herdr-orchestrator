from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from herdr_orchestrator.delivery_protocol import (
    DeliveryArtifactError,
    append_artifact_text,
    exclusive_file_claim,
    validate_artifact_path,
    write_artifact_text,
)
from herdr_orchestrator.observability import sanitize


@dataclass(frozen=True, slots=True)
class DeliveryEffect:
    key: str
    kind: str
    intent: dict[str, object]
    observe: Callable[[dict[str, object] | None, bool], DeliveryEffectObservation]
    apply: Callable[[], dict[str, object]]


class DeliveryEffectState(StrEnum):
    ABSENT = "absent"
    MATCHED = "matched"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class DeliveryEffectObservation:
    state: DeliveryEffectState
    details: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PendingDeliveryEffect:
    key: str
    kind: str
    intent: dict[str, object]
    started: bool


@dataclass(frozen=True, slots=True)
class ConfirmedDeliveryEffect:
    key: str
    kind: str
    details: dict[str, object]
    sequence: int


@dataclass(frozen=True, slots=True)
class _DeliveryOwner:
    owner_token: str
    status: str
    last_renewed_at: float
    lease_deadline: float


@dataclass(frozen=True, slots=True)
class _JournalEvent:
    sequence: int
    run_id: str
    owner_token: str
    recorded_at: float
    event: str
    operation_key: str | None
    effect_kind: str | None
    details: dict[str, object]


def _owner_token() -> str:
    return secrets.token_hex(16)


def _identity_payload(value: dict[str, object]) -> dict[str, object]:
    return dict(value)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate journal key")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


class DeliveryJournal:
    _OWNER_EVENTS = {
        "owner_acquired",
        "owner_recovered",
        "owner_released",
        "owner_renewed",
    }
    _EFFECT_EVENTS = {
        "effect_intent",
        "effect_started",
        "effect_confirmed",
        "effect_conflict",
    }
    _EFFECT_KINDS = {
        "agent.dispatch",
        "agent.respond",
        "git.integration.merge",
        "git.worktree.create",
        "receipt.ticket.accept",
        "repair.commit",
        "result.publish",
        "review.accept",
        "tracker.close",
        "tracker.publish",
    }

    def __init__(
        self,
        run_root: Path,
        run_id: str,
        owner_token: str,
        lease_seconds: float,
        *,
        error_type: type[Exception] = DeliveryArtifactError,
        clock: Callable[[], float] = time.time,
        payload_validator: Callable[[dict[str, object]], dict[str, object]] = _identity_payload,
    ) -> None:
        self.run_root = run_root
        self.run_id = run_id
        self.owner_token = owner_token
        self.lease_seconds = lease_seconds
        self.error_type = error_type
        self.clock = clock
        self.payload_validator = payload_validator
        self._events = self._load_events()
        self._lock = threading.Lock()

    @classmethod
    @contextmanager
    def claim(
        cls,
        run_root: Path,
        run_id: str,
        lease_seconds: float,
        *,
        error_type: type[Exception] = DeliveryArtifactError,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = _owner_token,
        payload_validator: Callable[[dict[str, object]], dict[str, object]] = _identity_payload,
    ) -> Iterator[DeliveryJournal]:
        with exclusive_file_claim(run_root / "run.lock", error_type=error_type):
            now = clock()
            journal = cls(
                run_root,
                run_id,
                "0" * 32,
                lease_seconds,
                error_type=error_type,
                clock=clock,
                payload_validator=payload_validator,
            )
            previous = journal._durable_owner()
            if previous is not None:
                journal._write_owner_snapshot(previous)
            if (
                previous is not None
                and previous.status == "active"
                and previous.lease_deadline > now
            ):
                raise error_type("delivery_run_active")
            owner_token = token_factory()
            if not re.fullmatch(r"[0-9a-f]{32}", owner_token):
                raise error_type("delivery_owner_token_invalid")
            journal.owner_token = owner_token
            details = journal._owner_details("active", now)
            if previous is not None and previous.status == "active":
                details.update(
                    {
                        "previous_owner": previous.owner_token,
                        "previous_lease_deadline": previous.lease_deadline,
                    }
                )
            journal._append("owner_acquired", details)
            journal._write_owner("active", now)
            try:
                yield journal
            finally:
                journal.release()

    def renew(self) -> None:
        with self._lock:
            now = self.clock()
            current = self._durable_owner()
            if (
                current is None
                or current.status != "active"
                or current.owner_token != self.owner_token
            ):
                raise self.error_type("delivery_owner_lost")
            self._append_locked(
                "owner_renewed",
                self._owner_details("active", now),
                now,
            )
            self._write_owner("active", now)

    def reconcile(self, effect: DeliveryEffect) -> dict[str, object]:
        self._validate_effect(effect)
        started, confirmation = self._prepare_effect(effect)
        expected = None if confirmation is None else dict(confirmation.details)
        observation, observed = self._observe(effect, expected, started)
        if confirmation is not None:
            return self._confirmed_observation(
                effect,
                confirmation,
                observation,
                observed,
                expected,
            )
        if observation.state is DeliveryEffectState.MATCHED:
            return self._confirm_observed(effect, observed)
        self._require_absent_observation(observation, observed)
        self._mark_started(effect)
        return self._apply_and_confirm(effect)

    def _validate_effect(self, effect: DeliveryEffect) -> None:
        if (
            re.fullmatch(r"[a-z][a-z0-9:.-]{0,127}", effect.key) is None
            or effect.kind not in self._EFFECT_KINDS
        ):
            raise self.error_type("delivery_journal_effect_invalid")

    def _prepare_effect(
        self,
        effect: DeliveryEffect,
    ) -> tuple[bool, _JournalEvent | None]:
        intent = self.payload_validator(effect.intent)
        self.renew()
        with self._lock:
            recorded, started, confirmation = self._operation(effect.key)
            if recorded is not None and (
                recorded.effect_kind != effect.kind or recorded.details != intent
            ):
                self._append_effect_locked(
                    "effect_conflict",
                    effect,
                    {"reason": "intent_mismatch"},
                    self.clock(),
                )
                raise self.error_type(f"delivery_recovery_conflict:{effect.kind}")
            if recorded is None:
                self._append_effect_locked("effect_intent", effect, intent, self.clock())
        return started, confirmation

    def _observe(
        self,
        effect: DeliveryEffect,
        expected: dict[str, object] | None,
        started: bool,
    ) -> tuple[DeliveryEffectObservation, dict[str, object] | None]:
        observation = effect.observe(expected, started)
        observed = (
            None if observation.details is None else self.payload_validator(observation.details)
        )
        if observation.state is DeliveryEffectState.CONFLICT:
            self._record_conflict(effect, "external_state_conflict")
        return observation, observed

    def _confirmed_observation(
        self,
        effect: DeliveryEffect,
        confirmation: _JournalEvent,
        observation: DeliveryEffectObservation,
        observed: dict[str, object] | None,
        expected: dict[str, object] | None,
    ) -> dict[str, object]:
        if observation.state is not DeliveryEffectState.MATCHED or observed != expected:
            self._record_conflict(effect, "confirmation_mismatch")
        return dict(confirmation.details)

    def _confirm_observed(
        self,
        effect: DeliveryEffect,
        observed: dict[str, object] | None,
    ) -> dict[str, object]:
        if observed is None:
            raise self.error_type("delivery_journal_observation_invalid")
        with self._lock:
            self._append_effect_locked(
                "effect_confirmed",
                effect,
                observed,
                self.clock(),
            )
        return observed

    def _require_absent_observation(
        self,
        observation: DeliveryEffectObservation,
        observed: dict[str, object] | None,
    ) -> None:
        if observation.state is not DeliveryEffectState.ABSENT or observed is not None:
            raise self.error_type("delivery_journal_observation_invalid")

    def _mark_started(self, effect: DeliveryEffect) -> None:
        with self._lock:
            _, started, confirmation = self._operation(effect.key)
            if confirmation is not None:
                raise self.error_type("delivery_journal_invalid")
            if not started:
                self._append_effect_locked("effect_started", effect, {}, self.clock())

    def _apply_and_confirm(self, effect: DeliveryEffect) -> dict[str, object]:
        result = self.payload_validator(effect.apply())
        observation, observed = self._observe(effect, result, True)
        if observation.state is not DeliveryEffectState.MATCHED or observed != result:
            self._record_conflict(effect, "postcondition_mismatch")
        self.renew()
        with self._lock:
            recorded, _, confirmation = self._operation(effect.key)
            if recorded is None:
                raise self.error_type("delivery_journal_invalid")
            if confirmation is not None:
                if confirmation.details != result:
                    raise self.error_type(f"delivery_recovery_conflict:{effect.kind}")
                return dict(confirmation.details)
            self._append_effect_locked("effect_confirmed", effect, result, self.clock())
        return result

    def _record_conflict(self, effect: DeliveryEffect, reason: str) -> None:
        with self._lock:
            self._append_effect_locked(
                "effect_conflict",
                effect,
                {"reason": reason},
                self.clock(),
            )
        raise self.error_type(f"delivery_recovery_conflict:{effect.kind}")

    def require_confirmed(self, operation_key: str) -> dict[str, object]:
        with self._lock:
            _, _, confirmation = self._operation(operation_key)
            if confirmation is None:
                raise self.error_type(f"delivery_effect_unconfirmed:{operation_key}")
            return dict(confirmation.details)

    def has_intent(self, operation_key: str) -> bool:
        with self._lock:
            intent, _, _ = self._operation(operation_key)
            return intent is not None

    def _intent_details(self, operation_key: str) -> dict[str, object] | None:
        with self._lock:
            intent, _, _ = self._operation(operation_key)
            return None if intent is None else dict(intent.details)

    def pending_effects(self, *, kind: str | None = None) -> tuple[PendingDeliveryEffect, ...]:
        with self._lock:
            keys = tuple(
                event.operation_key
                for event in self._events
                if event.event == "effect_intent" and event.operation_key is not None
            )
            pending: list[PendingDeliveryEffect] = []
            for key in keys:
                intent, started, confirmation = self._operation(key)
                if (
                    intent is not None
                    and confirmation is None
                    and intent.effect_kind is not None
                    and (kind is None or intent.effect_kind == kind)
                ):
                    pending.append(
                        PendingDeliveryEffect(
                            key,
                            intent.effect_kind,
                            dict(intent.details),
                            started,
                        )
                    )
            return tuple(pending)

    def latest_confirmation(
        self,
        *,
        kinds: frozenset[str],
    ) -> ConfirmedDeliveryEffect | None:
        with self._lock:
            for event in reversed(self._events):
                if (
                    event.event == "effect_confirmed"
                    and event.operation_key is not None
                    and event.effect_kind in kinds
                ):
                    return ConfirmedDeliveryEffect(
                        event.operation_key,
                        event.effect_kind,
                        dict(event.details),
                        event.sequence,
                    )
            return None

    def release(self) -> None:
        with self._lock:
            now = self.clock()
            current = self._durable_owner()
            if (
                current is None
                or current.status != "active"
                or current.owner_token != self.owner_token
            ):
                raise self.error_type("delivery_owner_lost")
            self._append_locked(
                "owner_released",
                self._owner_details("released", now),
                now,
            )
            self._write_owner("released", now)

    def _write_owner(self, status: str, observed_at: float) -> None:
        owner = _DeliveryOwner(
            self.owner_token,
            status,
            observed_at,
            (observed_at + self.lease_seconds if status == "active" else observed_at),
        )
        self._write_owner_snapshot(owner)

    def _write_owner_snapshot(self, owner: _DeliveryOwner) -> None:
        write_artifact_text(
            self.run_root / "run-owner.json",
            json.dumps(
                {
                    "version": 1,
                    "run_id": self.run_id,
                    "owner_token": owner.owner_token,
                    "status": owner.status,
                    "last_renewed_at": owner.last_renewed_at,
                    "lease_deadline": owner.lease_deadline,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            root=self.run_root,
            error_type=self.error_type,
        )

    def _owner_details(
        self,
        status: str,
        observed_at: float,
    ) -> dict[str, object]:
        return {
            "status": status,
            "last_renewed_at": observed_at,
            "lease_deadline": (
                observed_at + self.lease_seconds if status == "active" else observed_at
            ),
        }

    def _durable_owner(self) -> _DeliveryOwner | None:
        owner: _DeliveryOwner | None = None
        for event in self._events:
            if event.event not in {
                "owner_acquired",
                "owner_renewed",
                "owner_released",
            }:
                continue
            owner = _DeliveryOwner(
                event.owner_token,
                cast(str, event.details["status"]),
                float(cast(int | float, event.details["last_renewed_at"])),
                float(cast(int | float, event.details["lease_deadline"])),
            )
        return owner

    def _append(self, event: str, details: dict[str, object]) -> None:
        with self._lock:
            self._append_locked(event, details, self.clock())

    def _append_locked(
        self,
        event: str,
        details: dict[str, object],
        observed_at: float,
    ) -> None:
        if event not in self._OWNER_EVENTS:
            raise self.error_type("delivery_journal_event_invalid")
        sanitized = sanitize(details)
        if not isinstance(sanitized, dict):
            raise self.error_type("delivery_journal_event_invalid")
        self._persist_event(event, None, None, sanitized, observed_at)

    def _append_effect_locked(
        self,
        event: str,
        effect: DeliveryEffect,
        details: dict[str, object],
        observed_at: float,
    ) -> None:
        if event not in self._EFFECT_EVENTS:
            raise self.error_type("delivery_journal_event_invalid")
        self._persist_event(event, effect.key, effect.kind, details, observed_at)

    def _persist_event(
        self,
        event: str,
        operation_key: str | None,
        effect_kind: str | None,
        details: dict[str, object],
        observed_at: float,
    ) -> None:
        row = _JournalEvent(
            sequence=len(self._events) + 1,
            run_id=self.run_id,
            owner_token=self.owner_token,
            recorded_at=observed_at,
            event=event,
            operation_key=operation_key,
            effect_kind=effect_kind,
            details=details,
        )
        append_artifact_text(
            self.run_root / "journal.jsonl",
            json.dumps(
                {
                    "sequence": row.sequence,
                    "run_id": row.run_id,
                    "owner_token": row.owner_token,
                    "recorded_at": row.recorded_at,
                    "event": row.event,
                    "operation_key": row.operation_key,
                    "effect_kind": row.effect_kind,
                    "details": row.details,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            root=self.run_root,
            error_type=self.error_type,
        )
        self._events.append(row)

    def _operation(
        self,
        operation_key: str,
    ) -> tuple[_JournalEvent | None, bool, _JournalEvent | None]:
        intent: _JournalEvent | None = None
        started = False
        confirmation: _JournalEvent | None = None
        for event in self._events:
            if event.operation_key != operation_key:
                continue
            if event.event == "effect_intent":
                intent = event
            elif event.event == "effect_started":
                started = True
            elif event.event == "effect_confirmed":
                confirmation = event
        return intent, started, confirmation

    def _load_events(self) -> list[_JournalEvent]:
        path = self.run_root / "journal.jsonl"
        try:
            validate_artifact_path(path, root=self.run_root)
        except DeliveryArtifactError as exc:
            raise self.error_type("delivery_artifact_path_invalid") from exc
        if not path.is_file():
            return []
        payloads = self._read_event_payloads(path)
        operations: dict[str, tuple[str, dict[str, object], bool, bool]] = {}
        owner_state: list[str | None] = [None, None]
        return [
            self._decode_event(sequence, payload, operations, owner_state)
            for sequence, payload in enumerate(payloads, 1)
        ]

    def _read_event_payloads(self, path: Path) -> list[object]:
        try:
            return [
                json.loads(
                    line,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise self.error_type("delivery_journal_invalid") from exc

    def _decode_event(
        self,
        sequence: int,
        payload: object,
        operations: dict[str, tuple[str, dict[str, object], bool, bool]],
        owner_state: list[str | None],
    ) -> _JournalEvent:
        row = self._validated_event_payload(sequence, payload)
        event = cast(str, row["event"])
        operation_key = cast(str | None, row["operation_key"])
        effect_kind = cast(str | None, row["effect_kind"])
        details = dict(cast(dict[str, object], row["details"]))
        owner_token = cast(str, row["owner_token"])
        if event in self._OWNER_EVENTS:
            if operation_key is not None or effect_kind is not None:
                raise self.error_type("delivery_journal_invalid")
            self._reduce_owner_event(
                event,
                owner_token,
                details,
                owner_state,
            )
        elif event in self._EFFECT_EVENTS:
            if owner_state[0] != owner_token:
                raise self.error_type("delivery_journal_invalid")
            self._reduce_effect_event(
                event,
                operation_key,
                effect_kind,
                details,
                operations,
            )
        else:
            raise self.error_type("delivery_journal_invalid")
        return _JournalEvent(
            sequence=sequence,
            run_id=self.run_id,
            owner_token=owner_token,
            recorded_at=float(cast(int | float, row["recorded_at"])),
            event=event,
            operation_key=operation_key,
            effect_kind=effect_kind,
            details=details,
        )

    def _reduce_owner_event(
        self,
        event: str,
        owner_token: str,
        details: dict[str, object],
        state: list[str | None],
    ) -> None:
        active, pending = state
        if event == "owner_recovered":
            if active is None or owner_token == active:
                raise self.error_type("delivery_journal_invalid")
            state[1] = owner_token
            return
        self._validate_owner_details(event, details)
        if event == "owner_acquired":
            previous_owner = details.get("previous_owner")
            if active is None:
                if previous_owner is not None:
                    raise self.error_type("delivery_journal_invalid")
            elif previous_owner != active and pending != owner_token:
                raise self.error_type("delivery_journal_invalid")
            state[:] = [owner_token, None]
            return
        if active != owner_token:
            raise self.error_type("delivery_journal_invalid")
        if event == "owner_released":
            state[:] = [None, None]

    def _validate_owner_details(
        self,
        event: str,
        details: dict[str, object],
    ) -> None:
        base_keys = {"status", "last_renewed_at", "lease_deadline"}
        keys = set(details)
        if event == "owner_acquired":
            allowed = (
                base_keys,
                base_keys | {"previous_owner", "previous_lease_deadline"},
            )
            if keys not in allowed:
                raise self.error_type("delivery_journal_invalid")
            previous_owner = details.get("previous_owner")
            if previous_owner is not None and (
                not isinstance(previous_owner, str)
                or re.fullmatch(r"[0-9a-f]{32}", previous_owner) is None
                or not _finite_number(details.get("previous_lease_deadline"))
            ):
                raise self.error_type("delivery_journal_invalid")
        elif keys != base_keys:
            raise self.error_type("delivery_journal_invalid")
        status = details.get("status")
        renewed = details.get("last_renewed_at")
        deadline = details.get("lease_deadline")
        expected_status = "released" if event == "owner_released" else "active"
        if (
            status != expected_status
            or not _finite_number(renewed)
            or not _finite_number(deadline)
            or float(cast(int | float, deadline)) < float(cast(int | float, renewed))
            or (
                expected_status == "released"
                and float(cast(int | float, deadline)) != float(cast(int | float, renewed))
            )
        ):
            raise self.error_type("delivery_journal_invalid")

    def _validated_event_payload(
        self,
        sequence: int,
        payload: object,
    ) -> dict[str, object]:
        expected_keys = {
            "sequence",
            "run_id",
            "owner_token",
            "recorded_at",
            "event",
            "operation_key",
            "effect_kind",
            "details",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_keys
            or payload["sequence"] != sequence
            or payload["run_id"] != self.run_id
            or not isinstance(payload["owner_token"], str)
            or re.fullmatch(r"[0-9a-f]{32}", payload["owner_token"]) is None
            or not isinstance(payload["event"], str)
            or not isinstance(payload["details"], dict)
            or not _finite_number(payload["recorded_at"])
        ):
            raise self.error_type("delivery_journal_invalid")
        return payload

    def _reduce_effect_event(
        self,
        event: str,
        operation_key: object,
        effect_kind: object,
        details: dict[str, object],
        operations: dict[str, tuple[str, dict[str, object], bool, bool]],
    ) -> None:
        if (
            not isinstance(operation_key, str)
            or re.fullmatch(r"[a-z][a-z0-9:.-]{0,127}", operation_key) is None
            or not isinstance(effect_kind, str)
            or effect_kind not in self._EFFECT_KINDS
        ):
            raise self.error_type("delivery_journal_invalid")
        prior = operations.get(operation_key)
        if event == "effect_intent":
            if prior is not None:
                raise self.error_type("delivery_journal_invalid")
            operations[operation_key] = (effect_kind, details, False, False)
            return
        if prior is None or prior[0] != effect_kind:
            raise self.error_type("delivery_journal_invalid")
        if event == "effect_started":
            if prior[2] or prior[3] or details:
                raise self.error_type("delivery_journal_invalid")
            operations[operation_key] = (prior[0], prior[1], True, False)
        elif event == "effect_confirmed":
            if prior[3]:
                raise self.error_type("delivery_journal_invalid")
            operations[operation_key] = (prior[0], prior[1], prior[2], True)


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
