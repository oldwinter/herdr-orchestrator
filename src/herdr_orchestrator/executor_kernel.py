from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from herdr_orchestrator.executor_protocol import DefinitionError, canonical_json
from herdr_orchestrator.executor_store import (
    WORK_KERNEL_SCHEMA_VERSION,
    ExecutorStore,
    ExecutorStoreError,
    RunNotFoundError,
)


__all__ = [
    "BarrierRecord",
    "Barrier",
    "Dependency",
    "ClaimLostError",
    "ClaimedWork",
    "DependencyError",
    "DependencyRecord",
    "ExecutionKernel",
    "ExecutionKernelError",
    "KernelError",
    "ReplicaCapacityError",
    "ReplicaSlot",
    "ReplicaSlotRecord",
    "RunNotFoundError",
    "WorkItem",
    "WorkItemState",
    "WorkItemRecord",
    "ClaimedWorkItem",
    "WorkItemConflict",
    "WorkItemNotFound",
    "WorkState",
    "WORK_KERNEL_SCHEMA_VERSION",
]


MAX_ID_LENGTH = 128
MAX_WORK_ITEMS_PER_RUN = 10_000
MAX_REPLICA_SLOTS = 64


class WorkState(StrEnum):
    """String constants for the domain-neutral work state machine."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"

    @classmethod
    def terminal(cls) -> frozenset[WorkState]:
        return frozenset({cls.SUCCEEDED, cls.BLOCKED, cls.FAILED, cls.SKIPPED})

    @classmethod
    def all(cls) -> frozenset[WorkState]:
        return frozenset({cls.PENDING, cls.RUNNING, *cls.terminal()})


class KernelError(ExecutorStoreError):
    """Base error for durable work/dependency/claim operations."""

    code = "executor_kernel_error"


class WorkItemNotFound(KernelError):
    code = "work_item_not_found"


class WorkItemConflict(KernelError):
    code = "work_item_conflict"


class DependencyError(KernelError):
    code = "dependency_invalid"


class ClaimLostError(KernelError):
    code = "claim_lost"


class ReplicaCapacityError(KernelError):
    code = "replica_capacity_invalid"


ExecutionKernelError = KernelError


@dataclass(frozen=True, slots=True)
class DependencyRecord:
    run_id: str
    work_id: str
    depends_on_work_id: str
    ordinal: int

    @property
    def child_work_id(self) -> str:
        return self.work_id

    @property
    def parent_work_id(self) -> str:
        return self.depends_on_work_id

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "work_id": self.work_id,
            "depends_on_work_id": self.depends_on_work_id,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class WorkItem:
    run_id: str
    work_id: str
    work_key: str
    ordinal: int
    worker: str
    harness: str
    payload: Any
    state: str
    available_at: float
    claim_id: str | None
    replica_slot: str | None
    agent_name: str | None
    error_code: str | None
    created_at: float
    updated_at: float
    dependencies: tuple[str, ...] = ()
    barriers: tuple[str, ...] = ()
    ready: bool = False

    @property
    def id(self) -> str:
        return self.work_id

    @property
    def key(self) -> str:
        return self.work_key

    @property
    def is_ready(self) -> bool:
        return self.ready

    @property
    def status(self) -> str:
        return self.state

    @property
    def logical_id(self) -> str:
        return self.work_id

    @property
    def input(self) -> Any:
        return self.payload

    @property
    def dependency_ids(self) -> tuple[str, ...]:
        return self.dependencies

    @property
    def depends_on(self) -> tuple[str, ...]:
        return self.dependencies

    @property
    def barrier_ids(self) -> tuple[str, ...]:
        return self.barriers

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "work_id": self.work_id,
            "work_key": self.work_key,
            "ordinal": self.ordinal,
            "worker": self.worker,
            "harness": self.harness,
            "payload": self.payload,
            "state": self.state,
            "available_at": self.available_at,
            "claim_id": self.claim_id,
            "replica_slot": self.replica_slot,
            "agent_name": self.agent_name,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dependencies": list(self.dependencies),
            "barriers": list(self.barriers),
            "ready": self.ready,
        }


@dataclass(frozen=True, slots=True)
class ClaimedWork:
    run_id: str
    work_id: str
    work_key: str
    ordinal: int
    worker: str
    harness: str
    payload: Any
    claim_id: str
    replica_slot: str
    agent_name: str
    claimed_at: float

    @property
    def id(self) -> str:
        return self.work_id

    @property
    def slot_name(self) -> str:
        return self.replica_slot

    @property
    def claim(self) -> str:
        return self.claim_id

    @property
    def slot(self) -> str:
        return self.replica_slot

    @property
    def replica(self) -> str:
        return self.replica_slot

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "work_id": self.work_id,
            "work_key": self.work_key,
            "ordinal": self.ordinal,
            "worker": self.worker,
            "harness": self.harness,
            "payload": self.payload,
            "claim_id": self.claim_id,
            "replica_slot": self.replica_slot,
            "slot_name": self.replica_slot,
            "agent_name": self.agent_name,
            "state": WorkState.RUNNING.value,
            "claimed_at": self.claimed_at,
        }


@dataclass(frozen=True, slots=True)
class BarrierRecord:
    run_id: str
    barrier_id: str
    state: str
    ordinal: int
    required_work_ids: tuple[str, ...]
    release_work_ids: tuple[str, ...]
    error_code: str | None
    created_at: float
    updated_at: float

    @property
    def id(self) -> str:
        return self.barrier_id

    @property
    def ready(self) -> bool:
        return self.state == "succeeded"

    @property
    def complete(self) -> bool:
        return self.ready

    @property
    def members(self) -> tuple[str, ...]:
        return self.required_work_ids

    @property
    def releases(self) -> tuple[str, ...]:
        return self.release_work_ids

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "barrier_id": self.barrier_id,
            "state": self.state,
            "ready": self.ready,
            "ordinal": self.ordinal,
            "required_work_ids": list(self.required_work_ids),
            "release_work_ids": list(self.release_work_ids),
            "error_code": self.error_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ReplicaSlot:
    workflow: str
    harness: str
    slot_name: str
    slot_ordinal: int
    active_run_id: str | None
    active_work_id: str | None
    claim_id: str | None
    agent_name: str | None
    updated_at: float

    @property
    def available(self) -> bool:
        return self.active_run_id is None

    @property
    def state(self) -> str:
        return "available" if self.available else "occupied"

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow": self.workflow,
            "harness": self.harness,
            "slot_name": self.slot_name,
            "slot_ordinal": self.slot_ordinal,
            "active_run_id": self.active_run_id,
            "active_work_id": self.active_work_id,
            "claim_id": self.claim_id,
            "agent_name": self.agent_name,
            "available": self.available,
            "state": self.state,
            "updated_at": self.updated_at,
        }


# Descriptive aliases make the persistence record names explicit to
# executor callers without multiplying representations.
WorkItemRecord = WorkItem
ClaimedWorkItem = ClaimedWork
WorkItemState = WorkState
Barrier = BarrierRecord
ReplicaSlotRecord = ReplicaSlot
Dependency = DependencyRecord


class ExecutionKernel:
    """Domain-neutral durable work scheduling primitives.

    The kernel deliberately stores opaque JSON payloads and string worker
    identities.  It does not inspect or interpret executor-specific objects.
    Work readiness is derived from durable predecessor/barrier state; claims
    reserve a shared workflow/harness replica slot until a caller completes
    the claim.  Leases, retries, and fencing are added by the later attempt
    layer and therefore are not inferred from a settled claim here.
    """

    def __init__(
        self,
        store: ExecutorStore | Path | str,
        *,
        max_parallel: int = 16,
        replica_capacity: Mapping[str, int] | None = None,
        replica_slots: Mapping[str, Sequence[str]] | None = None,
        workspace: Path | str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(store, ExecutorStore):
            if isinstance(store, (Path, str)):
                store = ExecutorStore(store)
            else:
                raise TypeError("store_must_be_executor_store")
        if not isinstance(max_parallel, int) or isinstance(max_parallel, bool):
            raise ReplicaCapacityError("max_parallel_must_be_integer")
        if not 1 <= max_parallel <= MAX_REPLICA_SLOTS:
            raise ReplicaCapacityError("max_parallel_out_of_range")
        self.store = store
        self.max_parallel = max_parallel
        self._clock = clock
        self.workspace = None if workspace is None else Path(workspace).resolve()
        self._replica_capacity = _validate_capacity_mapping(replica_capacity)
        self._replica_slots = _validate_slot_mapping(replica_slots)
        all_configured_slots = [
            slot_name
            for names in self._replica_slots.values()
            for slot_name in names
        ]
        if len(all_configured_slots) != len(set(all_configured_slots)):
            raise ReplicaCapacityError("replica_slot_name_duplicate")
        for key, names in self._replica_slots.items():
            capacity = self._replica_capacity.get(key)
            if capacity is not None and capacity != len(names):
                raise ReplicaCapacityError("replica_capacity_slot_mismatch")
        self.store.initialize()

    def initialize(self) -> None:
        """Idempotently ensure the additive kernel tables exist."""

        self.store.initialize()

    # ------------------------------------------------------------------
    # Work creation and durable dependencies
    # ------------------------------------------------------------------

    def add_work_item(
        self,
        run_id: str,
        work_id: str | Mapping[str, Any] | None = None,
        *,
        work_key: str | None = None,
        ordinal: int | None = None,
        worker: str = "worker",
        harness: str = "default",
        payload: Any = None,
        depends_on: Iterable[str] = (),
        dependencies: Iterable[str] | None = None,
        depends_on_barriers: Iterable[str] = (),
        barrier_ids: Iterable[str] | None = None,
        available_at: float | None = None,
    ) -> WorkItem:
        """Create one opaque work item and its dependency edges.

        ``work_id`` is scoped to a run, so independent runs may use the same
        logical IDs.  ``work_key`` is a separate idempotency/readability key
        and defaults to ``work_id``.  Dependencies are checked for existence
        and cycles before the transaction commits.
        """

        if isinstance(work_id, Mapping):
            if (
                work_key is not None
                or payload is not None
                or worker != "worker"
                or harness != "default"
            ):
                raise WorkItemConflict("work_item_mapping_mixed_arguments")
            specification = dict(work_id)
            if "work_id" in specification and "id" in specification:
                raise WorkItemConflict("work_item_duplicate_id_argument")
            if "work_key" in specification and "key" in specification:
                raise WorkItemConflict("work_item_duplicate_key_argument")
            if "depends_on" in specification and "dependencies" in specification:
                raise WorkItemConflict("duplicate_dependency_argument")
            if (
                "depends_on_barriers" in specification
                and "barrier_ids" in specification
            ):
                raise WorkItemConflict("duplicate_barrier_argument")
            work_id = specification.pop("work_id", None)
            if work_id is None:
                work_id = specification.pop("id", None)
            work_key = specification.pop("work_key", None)
            if work_key is None:
                work_key = specification.pop("key", None)
            ordinal = specification.pop("ordinal", None)
            worker = specification.pop("worker", "worker")
            harness = specification.pop("harness", "default")
            payload = specification.pop("payload", None)
            dependencies = specification.pop("dependencies", None)
            depends_on = specification.pop("depends_on", ())
            depends_on_barriers = specification.pop("depends_on_barriers", ())
            if not depends_on_barriers:
                depends_on_barriers = specification.pop("barrier_ids", ())
            available_at = specification.pop("available_at", None)
            if specification:
                raise WorkItemConflict(
                    f"work_item_unknown_field: {sorted(specification)[0]}"
                )

        run_id = _identifier(run_id, "run_id")
        actual_work_id = _identifier(work_id, "work_id", allow_none=True)
        if actual_work_id is None:
            actual_work_id = f"work_{uuid.uuid4().hex}"
        actual_work_key = _identifier(work_key, "work_key", allow_none=True) or actual_work_id
        worker = _identifier(worker, "worker")
        harness = _identifier(harness, "harness")
        if dependencies is not None:
            if depends_on != ():
                raise DependencyError("duplicate_dependency_argument")
            depends_on = dependencies
        if barrier_ids is not None and depends_on_barriers != ():
            raise DependencyError("duplicate_barrier_argument")
        normalized_dependencies = _identifier_sequence(depends_on, "depends_on")
        normalized_barriers = _identifier_sequence(
            barrier_ids if barrier_ids is not None else depends_on_barriers,
            "depends_on_barriers",
        )
        if actual_work_id in normalized_dependencies:
            raise DependencyError("dependency_self_reference")
        ordinal_supplied = ordinal is not None
        if ordinal is not None and (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 0
        ):
            raise WorkItemConflict("work_item_ordinal_invalid")
        available_at_supplied = available_at is not None
        if available_at is None:
            available_at = self._clock()
        if (
            not isinstance(available_at, (int, float))
            or isinstance(available_at, bool)
            or not math.isfinite(float(available_at))
        ):
            raise WorkItemConflict("work_item_available_at_invalid")

        try:
            payload_json = canonical_json(payload)
        except (DefinitionError, TypeError, ValueError) as exc:
            raise WorkItemConflict(f"work_item_payload_invalid: {exc}") from exc

        with self.store._transaction() as connection:
            run = self._require_run(connection, run_id)
            count = connection.execute(
                "SELECT COUNT(*) AS total FROM executor_work_items WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if count is not None and int(count["total"]) >= MAX_WORK_ITEMS_PER_RUN:
                raise WorkItemConflict("work_item_limit_exceeded")
            if ordinal is None:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(ordinal) + 1, 0) AS next_ordinal
                    FROM executor_work_items WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                ordinal = int(row["next_ordinal"]) if row is not None else 0
            existing_rows = connection.execute(
                """
                SELECT * FROM executor_work_items
                WHERE run_id = ? AND (work_id = ? OR work_key = ?)
                ORDER BY work_id
                """,
                (run_id, actual_work_id, actual_work_key),
            ).fetchall()
            if existing_rows:
                if len(existing_rows) == 1:
                    existing = existing_rows[0]
                    existing_dependencies = tuple(
                        self._resolve_work_id(connection, run_id, dependency)
                        for dependency in normalized_dependencies
                    )
                    existing_barriers = tuple(normalized_barriers)
                    if (
                        str(existing["work_id"]) == actual_work_id
                        and str(existing["work_key"]) == actual_work_key
                        and self._same_work_definition(
                            connection,
                            existing,
                            worker=worker,
                            harness=harness,
                            payload_json=payload_json,
                            dependencies=existing_dependencies,
                            barriers=existing_barriers,
                            ordinal=ordinal if ordinal_supplied else None,
                            available_at=(
                                float(available_at)
                                if available_at_supplied
                                else None
                            ),
                        )
                    ):
                        self._ensure_slots(
                            connection,
                            str(run["workflow"]),
                            harness,
                            worker,
                            self._clock(),
                            None,
                        )
                        self._refresh_readiness(connection, run_id)
                        return self._work_record(connection, run_id, actual_work_id)
                raise WorkItemConflict(
                    f"{WorkItemConflict.code}: run_id={run_id} "
                    f"work_id={actual_work_id} work_key={actual_work_key}"
                )
            for dependency in normalized_dependencies:
                dependency_id = self._resolve_work_id(connection, run_id, dependency)
                if dependency_id == actual_work_id:
                    raise DependencyError("dependency_self_reference")
            for barrier_id in normalized_barriers:
                self._require_barrier(connection, run_id, barrier_id)
            if self._would_cycle(
                connection,
                run_id,
                actual_work_id,
                normalized_dependencies,
            ):
                raise DependencyError("dependency_cycle")
            now = self._clock()
            connection.execute(
                """
                INSERT INTO executor_work_items(
                    run_id, work_id, work_key, ordinal, worker, harness, payload_json,
                    state, available_at, claim_id, replica_slot, agent_name,
                    error_code, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    run_id,
                    actual_work_id,
                    actual_work_key,
                    ordinal,
                    worker,
                    harness,
                    payload_json,
                    WorkState.PENDING,
                    float(available_at),
                    now,
                    now,
                ),
            )
            for dependency_ordinal, dependency in enumerate(normalized_dependencies):
                dependency_id = self._resolve_work_id(connection, run_id, dependency)
                connection.execute(
                    """
                    INSERT INTO executor_dependencies(
                        run_id, work_id, depends_on_work_id, ordinal
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, actual_work_id, dependency_id, dependency_ordinal),
                )
            for barrier_ordinal, barrier_id in enumerate(normalized_barriers):
                connection.execute(
                    """
                    INSERT INTO executor_barrier_releases(
                        run_id, barrier_id, work_id, ordinal
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, barrier_id, actual_work_id, barrier_ordinal),
                )
            self._refresh_readiness(connection, run_id)
            self._ensure_slots(
                connection,
                str(run["workflow"]),
                harness,
                worker,
                self._clock(),
                None,
            )
            return self._work_record(connection, run_id, actual_work_id)

    # Names used by executor implementations can remain descriptive without
    # making the kernel a domain-specific queue.
    create_work_item = add_work_item
    add_work = add_work_item
    enqueue_work = add_work_item

    def add_work_items(
        self,
        run_id: str,
        items: Sequence[Mapping[str, Any]],
    ) -> list[WorkItem]:
        """Create a bounded batch atomically.

        Batch creation is useful for a frontier: every item is inserted in
        one transaction, then all dependency/barrier readiness is refreshed
        once at the same durable boundary.
        """

        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise WorkItemConflict("work_items_must_be_sequence")
        if not items:
            return []
        if len(items) > MAX_WORK_ITEMS_PER_RUN:
            raise WorkItemConflict("work_item_batch_limit_exceeded")
        run_id = _identifier(run_id, "run_id")
        prepared = [_prepare_work_mapping(item) for item in items]
        with self.store._transaction() as connection:
            run = self._require_run(connection, run_id)
            existing_count_row = connection.execute(
                "SELECT COUNT(*) AS total FROM executor_work_items WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            existing_count = int(existing_count_row["total"]) if existing_count_row else 0
            if existing_count + len(prepared) > MAX_WORK_ITEMS_PER_RUN:
                raise WorkItemConflict("work_item_limit_exceeded")

            normalized: list[dict[str, Any]] = []
            references: dict[str, str] = {}
            next_ordinal_row = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal) + 1, 0) AS next_ordinal
                FROM executor_work_items WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            next_ordinal = int(next_ordinal_row["next_ordinal"]) if next_ordinal_row else 0
            for item in prepared:
                item_id = _identifier(
                    item.get("work_id", item.get("id")),
                    "work_id",
                    allow_none=True,
                )
                if item_id is None:
                    item_id = f"work_{uuid.uuid4().hex}"
                item_key = _identifier(
                    item.get("work_key", item.get("key")),
                    "work_key",
                    allow_none=True,
                ) or item_id
                if item_id in references or item_key in references:
                    raise WorkItemConflict("work_item_duplicate_batch_identity")
                references[item_id] = item_id
                references[item_key] = item_id
                self._require_new_work_identity(connection, run_id, item_id, item_key)
                item_ordinal = item.get("ordinal")
                if item_ordinal is None:
                    item_ordinal = next_ordinal
                    next_ordinal += 1
                if (
                    not isinstance(item_ordinal, int)
                    or isinstance(item_ordinal, bool)
                    or item_ordinal < 0
                ):
                    raise WorkItemConflict("work_item_ordinal_invalid")
                item_worker = _identifier(item.get("worker", "worker"), "worker")
                item_harness = _identifier(item.get("harness", "default"), "harness")
                if "depends_on" in item and "dependencies" in item:
                    raise DependencyError("duplicate_dependency_argument")
                deps = _identifier_sequence(
                    item.get("depends_on", item.get("dependencies", ())),
                    "depends_on",
                )
                if "barrier_ids" in item and "depends_on_barriers" in item:
                    raise DependencyError("duplicate_barrier_argument")
                barriers = _identifier_sequence(
                    item.get(
                        "barrier_ids",
                        item.get("depends_on_barriers", ()),
                    ),
                    "depends_on_barriers",
                )
                if item_id in deps:
                    raise DependencyError("dependency_self_reference")
                payload = item.get("payload")
                try:
                    payload_json = canonical_json(payload)
                except (DefinitionError, TypeError, ValueError) as exc:
                    raise WorkItemConflict(f"work_item_payload_invalid: {exc}") from exc
                available = item.get("available_at", self._clock())
                if (
                    not isinstance(available, (int, float))
                    or isinstance(available, bool)
                    or not math.isfinite(float(available))
                ):
                    raise WorkItemConflict("work_item_available_at_invalid")
                normalized.append(
                    {
                        "work_id": item_id,
                        "work_key": item_key,
                        "ordinal": item_ordinal,
                        "worker": item_worker,
                        "harness": item_harness,
                        "payload_json": payload_json,
                        "available_at": float(available),
                        "depends_on": deps,
                        "barrier_ids": barriers,
                    }
                )
            for item in normalized:
                for dependency in item["depends_on"]:
                    if dependency not in references:
                        self._resolve_work_id(connection, run_id, dependency)
                for barrier_id in item["barrier_ids"]:
                    self._require_barrier(connection, run_id, barrier_id)
            # Check all graph edges, including edges between members of this
            # batch, before inserting any rows.
            for item in normalized:
                if self._would_cycle(
                    connection,
                    run_id,
                    item["work_id"],
                    item["depends_on"],
                    pending_items=normalized,
                ):
                    raise DependencyError("dependency_cycle")
            now = self._clock()
            for item in normalized:
                connection.execute(
                    """
                    INSERT INTO executor_work_items(
                        run_id, work_id, work_key, ordinal, worker, harness, payload_json,
                        state, available_at, claim_id, replica_slot, agent_name,
                        error_code, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        run_id,
                        item["work_id"],
                        item["work_key"],
                        item["ordinal"],
                        item["worker"],
                        item["harness"],
                        item["payload_json"],
                        WorkState.PENDING,
                        item["available_at"],
                        now,
                        now,
                    ),
                )
            for item in normalized:
                for dependency_ordinal, dependency in enumerate(item["depends_on"]):
                    dependency_id = self._resolve_work_id(
                        connection,
                        run_id,
                        dependency,
                    )
                    connection.execute(
                        """
                        INSERT INTO executor_dependencies(
                            run_id, work_id, depends_on_work_id, ordinal
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (run_id, item["work_id"], dependency_id, dependency_ordinal),
                    )
                for barrier_ordinal, barrier_id in enumerate(item["barrier_ids"]):
                    connection.execute(
                        """
                        INSERT INTO executor_barrier_releases(
                            run_id, barrier_id, work_id, ordinal
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (run_id, barrier_id, item["work_id"], barrier_ordinal),
                    )
                self._ensure_slots(
                    connection,
                    str(run["workflow"]),
                    item["harness"],
                    item["worker"],
                    now,
                    None,
                )
            self._refresh_readiness(connection, run_id)
            return [
                self._work_record(connection, run_id, item["work_id"])
                for item in normalized
            ]

    create_work_items = add_work_items

    def add_dependency(
        self,
        run_id: str,
        work_id: str,
        depends_on: str,
        *,
        ordinal: int | None = None,
    ) -> WorkItem:
        run_id = _identifier(run_id, "run_id")
        work_id = _identifier(work_id, "work_id")
        depends_on = _identifier(depends_on, "depends_on_work_id")
        if work_id == depends_on:
            raise DependencyError("dependency_self_reference")
        if ordinal is not None and (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 0
        ):
            raise DependencyError("dependency_ordinal_invalid")
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            self._require_work(connection, run_id, work_id)
            parent_id = self._resolve_work_id(connection, run_id, depends_on)
            duplicate = connection.execute(
                """
                SELECT 1 FROM executor_dependencies
                WHERE run_id = ? AND work_id = ? AND depends_on_work_id = ?
                """,
                (run_id, work_id, parent_id),
            ).fetchone()
            if duplicate is not None:
                return self._work_record(connection, run_id, work_id)
            if self._would_cycle(connection, run_id, work_id, (parent_id,)):
                raise DependencyError("dependency_cycle")
            if ordinal is None:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(ordinal) + 1, 0) AS next_ordinal
                    FROM executor_dependencies WHERE run_id = ? AND work_id = ?
                    """,
                    (run_id, work_id),
                ).fetchone()
                ordinal = int(row["next_ordinal"]) if row else 0
            connection.execute(
                """
                INSERT INTO executor_dependencies(
                    run_id, work_id, depends_on_work_id, ordinal
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, work_id, parent_id, ordinal),
            )
            self._refresh_readiness(connection, run_id)
            return self._work_record(connection, run_id, work_id)

    add_work_dependency = add_dependency

    def create_barrier(
        self,
        run_id: str,
        barrier_id: str,
        required_work_ids: Iterable[str] = (),
        release_work_ids: Iterable[str] = (),
        *,
        member_work_ids: Iterable[str] | None = None,
        ordinal: int | None = None,
    ) -> BarrierRecord:
        """Create a durable fan-in barrier and its release edges."""

        run_id = _identifier(run_id, "run_id")
        barrier_id = _identifier(barrier_id, "barrier_id")
        if member_work_ids is not None:
            required_work_ids = member_work_ids
        requested_required = _identifier_sequence(required_work_ids, "required_work_ids")
        requested_releases = _identifier_sequence(release_work_ids, "release_work_ids")
        if ordinal is not None and (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 0
        ):
            raise DependencyError("barrier_ordinal_invalid")
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            required = tuple(
                self._resolve_work_id(connection, run_id, work_id)
                for work_id in requested_required
            )
            releases = tuple(
                self._resolve_work_id(connection, run_id, work_id)
                for work_id in requested_releases
            )
            if set(required) & set(releases):
                raise DependencyError("barrier_member_release_overlap")
            if any(
                self._work_depends_on(
                    connection,
                    run_id,
                    member_work_id,
                    release_work_id,
                )
                for member_work_id in required
                for release_work_id in releases
            ):
                raise DependencyError("barrier_cycle")
            existing = connection.execute(
                """
                SELECT * FROM executor_barriers
                WHERE run_id = ? AND barrier_id = ?
                """,
                (run_id, barrier_id),
            ).fetchone()
            if existing is not None:
                existing_record = self._barrier_record(connection, existing)
                if (
                    existing_record.required_work_ids != required
                    or existing_record.release_work_ids != releases
                ):
                    raise WorkItemConflict("barrier_definition_conflict")
                return existing_record
            if ordinal is None:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(ordinal) + 1, 0) AS next_ordinal
                    FROM executor_barriers WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                ordinal = int(row["next_ordinal"]) if row else 0
            now = self._clock()
            connection.execute(
                """
                INSERT INTO executor_barriers(
                    run_id, barrier_id, state, ordinal, error_code, created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, NULL, ?, ?)
                """,
                (run_id, barrier_id, ordinal, now, now),
            )
            for member_ordinal, work_id in enumerate(required):
                connection.execute(
                    """
                    INSERT INTO executor_barrier_members(
                        run_id, barrier_id, work_id, ordinal
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, barrier_id, work_id, member_ordinal),
                )
            for release_ordinal, work_id in enumerate(releases):
                connection.execute(
                    """
                    INSERT INTO executor_barrier_releases(
                        run_id, barrier_id, work_id, ordinal
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, barrier_id, work_id, release_ordinal),
                )
            self._refresh_readiness(connection, run_id)
            row = connection.execute(
                """
                SELECT * FROM executor_barriers
                WHERE run_id = ? AND barrier_id = ?
                """,
                (run_id, barrier_id),
            ).fetchone()
            if row is None:
                raise KernelError("barrier_creation_failed")
            return self._barrier_record(connection, row)

    add_barrier = create_barrier

    def add_barrier_member(
        self,
        run_id: str,
        barrier_id: str,
        work_id: str,
        *,
        ordinal: int | None = None,
    ) -> BarrierRecord:
        return self._add_barrier_edge(
            run_id,
            barrier_id,
            work_id,
            member=True,
            ordinal=ordinal,
        )

    def add_barrier_release(
        self,
        run_id: str,
        barrier_id: str,
        work_id: str,
        *,
        ordinal: int | None = None,
    ) -> BarrierRecord:
        return self._add_barrier_edge(
            run_id,
            barrier_id,
            work_id,
            member=False,
            ordinal=ordinal,
        )

    # ------------------------------------------------------------------
    # Readiness and claims
    # ------------------------------------------------------------------

    def ready_work_items(
        self,
        run_id: str,
        *,
        limit: int | None = None,
    ) -> list[WorkItem]:
        run_id = _identifier(run_id, "run_id")
        limit = _validate_limit(limit, MAX_WORK_ITEMS_PER_RUN)
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            self._refresh_readiness(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM executor_work_items
                WHERE run_id = ? AND state = ? AND available_at <= ?
                ORDER BY ordinal, created_at, work_id
                """,
                (run_id, WorkState.PENDING, self._clock()),
            ).fetchall()
            records = [
                self._work_record(connection, run_id, str(row["work_id"]), ready=True)
                for row in rows
                if self._is_ready(connection, run_id, str(row["work_id"]))
            ]
            return records[:limit] if limit is not None else records

    ready_work = ready_work_items
    ready = ready_work_items

    def claim_ready(
        self,
        run_id: str,
        *,
        limit: int | None = None,
    ) -> list[ClaimedWork]:
        run_id = _identifier(run_id, "run_id")
        return self._claim_candidates(run_id=run_id, limit=limit)

    claim_work = claim_ready
    claim = claim_ready

    def claim_ready_for_workflow(
        self,
        workflow: str,
        *,
        limit: int | None = None,
        run_ids: Iterable[str] | None = None,
    ) -> list[ClaimedWork]:
        workflow = _identifier(workflow, "workflow")
        normalized_runs = (
            None
            if run_ids is None
            else _identifier_sequence(run_ids, "run_ids")
        )
        return self._claim_candidates(
            workflow=workflow,
            run_ids=normalized_runs,
            limit=limit,
        )

    claim_work_for_workflow = claim_ready_for_workflow
    claim_ready_workflow = claim_ready_for_workflow

    def complete_work_item(
        self,
        claim: ClaimedWork | str,
        work_id: str | None = None,
        *,
        state: str = WorkState.SUCCEEDED,
        error_code: str | None = None,
    ) -> WorkItem:
        """Settle a current claim and atomically release its replica slot."""

        if isinstance(claim, ClaimedWork):
            run_id = claim.run_id
            actual_work_id = claim.work_id
            claim_id = claim.claim_id
        else:
            run_id = _identifier(claim, "run_id")
            actual_work_id = _identifier(work_id, "work_id")
            claim_id = None
        if state not in {item.value for item in WorkState.terminal()}:
            raise KernelError(f"work_terminal_state_invalid: {state}")
        if error_code is not None:
            error_code = _identifier(error_code, "error_code")
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            row = connection.execute(
                """
                SELECT * FROM executor_work_items
                WHERE run_id = ? AND work_id = ?
                """,
                (run_id, actual_work_id),
            ).fetchone()
            if row is None:
                raise WorkItemNotFound(f"work_item_not_found: {actual_work_id}")
            current_claim = row["claim_id"]
            if (
                claim_id is not None
                and str(row["state"]) in {item.value for item in WorkState.terminal()}
                and current_claim == claim_id
            ):
                return self._work_record(connection, run_id, actual_work_id)
            if str(row["state"]) != WorkState.RUNNING or (
                claim_id is not None and current_claim != claim_id
            ):
                raise ClaimLostError(
                    f"{ClaimLostError.code}: run_id={run_id} work_id={actual_work_id}"
                )
            now = self._clock()
            connection.execute(
                """
                UPDATE executor_work_items
                SET state = ?, error_code = ?, updated_at = ?
                WHERE run_id = ? AND work_id = ? AND state = ? AND claim_id = ?
                """,
                (
                    state,
                    error_code,
                    now,
                    run_id,
                    actual_work_id,
                    WorkState.RUNNING,
                    current_claim,
                ),
            )
            if row["replica_slot"] is not None:
                connection.execute(
                    """
                    UPDATE executor_replica_slots
                    SET active_run_id = NULL, active_work_id = NULL,
                        claim_id = NULL, agent_name = NULL, updated_at = ?
                    WHERE workflow = (SELECT workflow FROM executor_runs WHERE run_id = ?)
                      AND harness = ? AND slot_name = ?
                      AND active_run_id = ? AND active_work_id = ?
                      AND claim_id = ?
                    """,
                    (
                        now,
                        run_id,
                        str(row["harness"]),
                        str(row["replica_slot"]),
                        run_id,
                        actual_work_id,
                        current_claim,
                    ),
                )
            self._refresh_readiness(connection, run_id)
            return self._work_record(connection, run_id, actual_work_id)

    complete_work = complete_work_item
    settle_work_item = complete_work_item

    def fail_work_item(
        self,
        claim: ClaimedWork,
        *,
        error_code: str = "work_failed",
    ) -> WorkItem:
        return self.complete_work_item(
            claim,
            state=WorkState.FAILED,
            error_code=error_code,
        )

    def block_work_item(
        self,
        claim: ClaimedWork,
        *,
        error_code: str = "work_blocked",
    ) -> WorkItem:
        return self.complete_work_item(
            claim,
            state=WorkState.BLOCKED,
            error_code=error_code,
        )

    def skip_work_item(
        self,
        claim: ClaimedWork,
        *,
        error_code: str = "work_skipped",
    ) -> WorkItem:
        return self.complete_work_item(
            claim,
            state=WorkState.SKIPPED,
            error_code=error_code,
        )

    # ------------------------------------------------------------------
    # Durable views and replica capacity
    # ------------------------------------------------------------------

    def get_work_item(self, run_id: str, work_id: str) -> WorkItem:
        run_id = _identifier(run_id, "run_id")
        work_id = _identifier(work_id, "work_id")
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            self._refresh_readiness(connection, run_id)
            return self._work_record(connection, run_id, work_id)

    work_item = get_work_item

    def list_work_items(self, run_id: str) -> list[WorkItem]:
        run_id = _identifier(run_id, "run_id")
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            self._refresh_readiness(connection, run_id)
            rows = connection.execute(
                """
                SELECT work_id FROM executor_work_items
                WHERE run_id = ?
                ORDER BY ordinal, created_at, work_id
                """,
                (run_id,),
            ).fetchall()
            return [
                self._work_record(connection, run_id, str(row["work_id"]))
                for row in rows
            ]

    work_items = list_work_items

    def work_state_counts(self, run_id: str) -> dict[str, int]:
        run_id = _identifier(run_id, "run_id")
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            self._refresh_readiness(connection, run_id)
            counts = {state.value: 0 for state in WorkState}
            rows = connection.execute(
                """
                SELECT state, COUNT(*) AS total
                FROM executor_work_items
                WHERE run_id = ?
                GROUP BY state
                """,
                (run_id,),
            ).fetchall()
            for row in rows:
                counts[str(row["state"])] = int(row["total"])
            counts["ready"] = sum(
                1
                for row in connection.execute(
                    """
                    SELECT work_id FROM executor_work_items
                    WHERE run_id = ? AND state = ? AND available_at <= ?
                    """,
                    (run_id, WorkState.PENDING, self._clock()),
                ).fetchall()
                if self._is_ready(connection, run_id, str(row["work_id"]))
            )
            return counts

    def get_barrier(self, run_id: str, barrier_id: str) -> BarrierRecord:
        run_id = _identifier(run_id, "run_id")
        barrier_id = _identifier(barrier_id, "barrier_id")
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            self._refresh_readiness(connection, run_id)
            row = connection.execute(
                """
                SELECT * FROM executor_barriers
                WHERE run_id = ? AND barrier_id = ?
                """,
                (run_id, barrier_id),
            ).fetchone()
            if row is None:
                raise KernelError(f"barrier_not_found: {barrier_id}")
            return self._barrier_record(connection, row)

    barrier = get_barrier

    def list_barriers(self, run_id: str) -> list[BarrierRecord]:
        run_id = _identifier(run_id, "run_id")
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            self._refresh_readiness(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM executor_barriers
                WHERE run_id = ?
                ORDER BY ordinal, created_at, barrier_id
                """,
                (run_id,),
            ).fetchall()
            return [self._barrier_record(connection, row) for row in rows]

    barriers = list_barriers

    def list_dependencies(self, run_id: str) -> list[dict[str, object]]:
        run_id = _identifier(run_id, "run_id")
        with self.store._connect() as connection:
            self._require_run(connection, run_id)
            rows = connection.execute(
                """
                SELECT work_id, depends_on_work_id, ordinal
                FROM executor_dependencies
                WHERE run_id = ?
                ORDER BY work_id, ordinal, depends_on_work_id
                """,
                (run_id,),
            ).fetchall()
            return [
                {
                    "run_id": run_id,
                    "work_id": str(row["work_id"]),
                    "depends_on_work_id": str(row["depends_on_work_id"]),
                    "ordinal": int(row["ordinal"]),
                }
                for row in rows
            ]

    dependencies = list_dependencies

    def dependency_records(self, run_id: str) -> list[DependencyRecord]:
        return [
            DependencyRecord(
                run_id=str(item["run_id"]),
                work_id=str(item["work_id"]),
                depends_on_work_id=str(item["depends_on_work_id"]),
                ordinal=int(item["ordinal"]),
            )
            for item in self.list_dependencies(run_id)
        ]

    def replica_slots(
        self,
        workflow: str,
        *,
        harness: str | None = None,
    ) -> list[ReplicaSlot]:
        workflow = _identifier(workflow, "workflow")
        if harness is not None:
            harness = _identifier(harness, "harness")
        with self.store._connect() as connection:
            if harness is None:
                rows = connection.execute(
                    """
                    SELECT * FROM executor_replica_slots
                    WHERE workflow = ?
                    ORDER BY harness, slot_ordinal, slot_name
                    """,
                    (workflow,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM executor_replica_slots
                    WHERE workflow = ? AND harness = ?
                    ORDER BY slot_ordinal, slot_name
                    """,
                    (workflow, harness),
                ).fetchall()
            return [self._slot_record(row) for row in rows]

    list_replica_slots = replica_slots

    def inspect_run(self, run_id: str) -> dict[str, object]:
        run_id = _identifier(run_id, "run_id")
        run = self.store.require_run(run_id)
        work = self.list_work_items(run_id)
        barriers = self.list_barriers(run_id)
        dependencies = self.list_dependencies(run_id)
        slots = self.replica_slots(run.workflow)
        return {
            **run.to_dict(),
            "work_counts": self.work_state_counts(run_id),
            "work_items": [item.to_dict() for item in work],
            "dependencies": dependencies,
            "barriers": [barrier.to_dict() for barrier in barriers],
            "replica_slots": [slot.to_dict() for slot in slots],
        }

    inspect = inspect_run

    def register_replica_capacity(
        self,
        workflow: str,
        harness: str,
        capacity: int,
        *,
        slot_names: Sequence[str] | None = None,
    ) -> list[ReplicaSlot]:
        workflow = _identifier(workflow, "workflow")
        harness = _identifier(harness, "harness")
        if (
            not isinstance(capacity, int)
            or isinstance(capacity, bool)
            or not 1 <= capacity <= MAX_REPLICA_SLOTS
        ):
            raise ReplicaCapacityError("replica_capacity_out_of_range")
        names = (
            tuple(_identifier_sequence(slot_names, "slot_names"))
            if slot_names is not None
            else self._generated_slot_names(workflow, harness, capacity)
        )
        if len(names) != capacity or len(set(names)) != len(names):
            raise ReplicaCapacityError("replica_slot_count_mismatch")
        with self.store._transaction() as connection:
            existing_rows = connection.execute(
                """
                SELECT slot_name FROM executor_replica_slots
                WHERE workflow = ? AND harness = ?
                ORDER BY slot_ordinal, slot_name
                """,
                (workflow, harness),
            ).fetchall()
            existing = tuple(str(row["slot_name"]) for row in existing_rows)
            if existing and existing != names:
                raise ReplicaCapacityError("replica_capacity_conflict")
            self._ensure_slots(connection, workflow, harness, None, self._clock(), names)
        return self.replica_slots(workflow, harness=harness)

    # ------------------------------------------------------------------
    # Internal transaction helpers
    # ------------------------------------------------------------------

    def _claim_candidates(
        self,
        *,
        run_id: str | None = None,
        workflow: str | None = None,
        run_ids: Sequence[str] | None = None,
        limit: int | None,
    ) -> list[ClaimedWork]:
        if run_id is None and workflow is None:
            raise KernelError("claim_scope_required")
        if run_id is not None:
            run_id = _identifier(run_id, "run_id")
        if workflow is not None:
            workflow = _identifier(workflow, "workflow")
        limit = _validate_limit(limit, self.max_parallel)
        requested_limit = self.max_parallel if limit is None else limit
        with self.store._transaction() as connection:
            if run_id is not None:
                run = self._require_run(connection, run_id)
                workflow = str(run["workflow"])
                run_ids = (run_id,)
            else:
                if run_ids is not None:
                    run_ids = _identifier_sequence(run_ids, "run_ids")
                    if not run_ids:
                        return []
                run_rows = connection.execute(
                    """
                    SELECT run_id FROM executor_runs
                    WHERE workflow = ?
                    ORDER BY created_at, run_id
                    """,
                    (workflow,),
                ).fetchall()
                available_run_ids = [str(row["run_id"]) for row in run_rows]
                if run_ids is not None:
                    allowed = set(run_ids)
                    available_run_ids = [
                        value for value in available_run_ids if value in allowed
                    ]
                run_ids = tuple(available_run_ids)
            if not run_ids:
                return []
            # A paused/terminal run is not eligible for new claims.  This is
            # a mechanical safeguard; executor-specific controls own the
            # public meaning of those states.
            run_state_rows = connection.execute(
                f"""
                SELECT run_id, state FROM executor_runs
                WHERE workflow = ? AND run_id IN ({",".join("?" for _ in run_ids)})
                """,
                (workflow, *run_ids),
            ).fetchall()
            eligible_runs = {
                str(row["run_id"])
                for row in run_state_rows
                if str(row["state"]) in {"pending", "running"}
            }
            if not eligible_runs:
                return []
            active_row = connection.execute(
                """
                SELECT COUNT(*) AS total FROM executor_work_items w
                JOIN executor_runs r ON r.run_id = w.run_id
                WHERE r.workflow = ? AND w.state = ?
                """,
                (workflow, WorkState.RUNNING),
            ).fetchone()
            active_count = int(active_row["total"]) if active_row else 0
            remaining = min(requested_limit, self.max_parallel - active_count)
            if remaining <= 0:
                return []
            now = self._clock()
            claims: list[ClaimedWork] = []
            # Readiness refresh is done for each run before selecting work.
            # Refreshing before candidate query would require a second query;
            # refresh and re-query keeps the claim transaction atomic.
            for candidate_run_id in sorted(eligible_runs):
                self._refresh_readiness(connection, candidate_run_id)
            candidates = connection.execute(
                f"""
                SELECT w.*, r.created_at AS run_created_at
                FROM executor_work_items w
                JOIN executor_runs r ON r.run_id = w.run_id
                WHERE r.workflow = ?
                  AND w.run_id IN ({",".join("?" for _ in eligible_runs)})
                  AND w.state = ?
                  AND w.available_at <= ?
                ORDER BY r.created_at, r.run_id, w.ordinal, w.created_at, w.work_id
                """,
                (
                    workflow,
                    *sorted(eligible_runs),
                    WorkState.PENDING,
                    now,
                ),
            ).fetchall()
            busy_slots: dict[str, set[str]] = {}
            for row in connection.execute(
                """
                SELECT harness, slot_name FROM executor_replica_slots
                WHERE workflow = ? AND active_run_id IS NOT NULL
                """,
                (workflow,),
            ).fetchall():
                busy_slots.setdefault(str(row["harness"]), set()).add(str(row["slot_name"]))

            for row in candidates:
                if len(claims) >= remaining:
                    break
                candidate_run_id = str(row["run_id"])
                candidate_work_id = str(row["work_id"])
                if not self._is_ready(connection, candidate_run_id, candidate_work_id):
                    continue
                harness = str(row["harness"])
                worker = str(row["worker"])
                slots = self._ensure_slots(
                    connection,
                    workflow,
                    harness,
                    worker,
                    now,
                    None,
                )
                used = busy_slots.setdefault(harness, set())
                slot = next((item for item in slots if item not in used), None)
                if slot is None:
                    continue
                claim_id = f"claim_{uuid.uuid4().hex}"
                agent_name = slot
                updated = connection.execute(
                    """
                    UPDATE executor_work_items
                    SET state = ?, claim_id = ?, replica_slot = ?, agent_name = ?,
                        error_code = NULL, updated_at = ?
                    WHERE run_id = ? AND work_id = ? AND state = ?
                    """,
                    (
                        WorkState.RUNNING,
                        claim_id,
                        slot,
                        agent_name,
                        now,
                        candidate_run_id,
                        candidate_work_id,
                        WorkState.PENDING,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                slot_updated = connection.execute(
                    """
                    UPDATE executor_replica_slots
                    SET active_run_id = ?, active_work_id = ?, claim_id = ?,
                        agent_name = ?, updated_at = ?
                    WHERE workflow = ? AND harness = ? AND slot_name = ?
                      AND active_run_id IS NULL
                    """,
                    (
                        candidate_run_id,
                        candidate_work_id,
                        claim_id,
                        agent_name,
                        now,
                        workflow,
                        harness,
                        slot,
                    ),
                )
                if slot_updated.rowcount != 1:
                    connection.execute(
                        """
                        UPDATE executor_work_items
                        SET state = ?, claim_id = NULL, replica_slot = NULL,
                            agent_name = NULL, updated_at = ?
                        WHERE run_id = ? AND work_id = ? AND claim_id = ?
                        """,
                        (
                            WorkState.PENDING,
                            now,
                            candidate_run_id,
                            candidate_work_id,
                            claim_id,
                        ),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE executor_runs
                    SET state = CASE WHEN state = 'pending' THEN 'running' ELSE state END,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (now, candidate_run_id),
                )
                used.add(slot)
                claims.append(
                    ClaimedWork(
                        run_id=candidate_run_id,
                        work_id=candidate_work_id,
                        work_key=str(row["work_key"]),
                        ordinal=int(row["ordinal"]),
                        worker=worker,
                        harness=harness,
                        payload=json.loads(str(row["payload_json"])),
                        claim_id=claim_id,
                        replica_slot=slot,
                        agent_name=agent_name,
                        claimed_at=now,
                    )
                )
            return claims

    def _refresh_readiness(self, connection: Any, run_id: str) -> None:
        """Propagate terminal dependency/barrier outcomes to a fixed point."""

        rows = connection.execute(
            "SELECT COUNT(*) AS total FROM executor_work_items WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        work_count = int(rows["total"]) if rows else 0
        barrier_rows = connection.execute(
            "SELECT COUNT(*) AS total FROM executor_barriers WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        barrier_count = int(barrier_rows["total"]) if barrier_rows else 0
        for _ in range(work_count + barrier_count + 1):
            changed = False
            barriers = connection.execute(
                """
                SELECT * FROM executor_barriers
                WHERE run_id = ?
                ORDER BY ordinal, created_at, barrier_id
                """,
                (run_id,),
            ).fetchall()
            for barrier in barriers:
                barrier_id = str(barrier["barrier_id"])
                members = connection.execute(
                    """
                    SELECT w.state
                    FROM executor_barrier_members m
                    JOIN executor_work_items w
                      ON w.run_id = m.run_id AND w.work_id = m.work_id
                    WHERE m.run_id = ? AND m.barrier_id = ?
                    ORDER BY m.ordinal, m.work_id
                    """,
                    (run_id, barrier_id),
                ).fetchall()
                states = [str(member["state"]) for member in members]
                if states and all(state == WorkState.SUCCEEDED for state in states):
                    desired, error = "succeeded", None
                elif any(
                    state in {WorkState.FAILED, WorkState.BLOCKED, WorkState.SKIPPED}
                    for state in states
                ):
                    desired, error = "failed", "barrier_dependency_failed"
                elif not states:
                    desired, error = "succeeded", None
                else:
                    desired, error = "pending", None
                if str(barrier["state"]) != desired or barrier["error_code"] != error:
                    connection.execute(
                        """
                        UPDATE executor_barriers
                        SET state = ?, error_code = ?, updated_at = ?
                        WHERE run_id = ? AND barrier_id = ?
                        """,
                        (desired, error, self._clock(), run_id, barrier_id),
                    )
                    changed = True

            pending = connection.execute(
                """
                SELECT work_id FROM executor_work_items
                WHERE run_id = ? AND state = ?
                ORDER BY ordinal, created_at, work_id
                """,
                (run_id, WorkState.PENDING),
            ).fetchall()
            for row in pending:
                work_id = str(row["work_id"])
                dependency_states = connection.execute(
                    """
                    SELECT parent.state
                    FROM executor_dependencies d
                    JOIN executor_work_items parent
                      ON parent.run_id = d.run_id
                     AND parent.work_id = d.depends_on_work_id
                    WHERE d.run_id = ? AND d.work_id = ?
                    """,
                    (run_id, work_id),
                ).fetchall()
                barrier_states = connection.execute(
                    """
                    SELECT b.state
                    FROM executor_barrier_releases r
                    JOIN executor_barriers b
                      ON b.run_id = r.run_id AND b.barrier_id = r.barrier_id
                    WHERE r.run_id = ? AND r.work_id = ?
                    """,
                    (run_id, work_id),
                ).fetchall()
                failed_dependency = any(
                    str(item["state"]) in {
                        WorkState.FAILED,
                        WorkState.BLOCKED,
                        WorkState.SKIPPED,
                    }
                    for item in (*dependency_states, *barrier_states)
                )
                if failed_dependency:
                    connection.execute(
                        """
                        UPDATE executor_work_items
                        SET state = ?, error_code = ?, updated_at = ?
                        WHERE run_id = ? AND work_id = ? AND state = ?
                        """,
                        (
                            WorkState.SKIPPED,
                            "dependency_failed",
                            self._clock(),
                            run_id,
                            work_id,
                            WorkState.PENDING,
                        ),
                    )
                    changed = True
            if not changed:
                return
        raise KernelError("readiness_propagation_bound_exceeded")

    def _is_ready(self, connection: Any, run_id: str, work_id: str) -> bool:
        row = connection.execute(
            """
            SELECT state, available_at FROM executor_work_items
            WHERE run_id = ? AND work_id = ?
            """,
            (run_id, work_id),
        ).fetchone()
        if row is None or str(row["state"]) != WorkState.PENDING:
            return False
        if float(row["available_at"]) > self._clock():
            return False
        dependency_states = connection.execute(
            """
            SELECT parent.state
            FROM executor_dependencies d
            JOIN executor_work_items parent
              ON parent.run_id = d.run_id
             AND parent.work_id = d.depends_on_work_id
            WHERE d.run_id = ? AND d.work_id = ?
            """,
            (run_id, work_id),
        ).fetchall()
        barrier_states = connection.execute(
            """
            SELECT b.state
            FROM executor_barrier_releases r
            JOIN executor_barriers b
              ON b.run_id = r.run_id AND b.barrier_id = r.barrier_id
            WHERE r.run_id = ? AND r.work_id = ?
            """,
            (run_id, work_id),
        ).fetchall()
        return all(
            str(item["state"]) == WorkState.SUCCEEDED
            for item in (*dependency_states, *barrier_states)
        )

    def _work_record(
        self,
        connection: Any,
        run_id: str,
        work_id: str,
        *,
        ready: bool | None = None,
    ) -> WorkItem:
        row = connection.execute(
            """
            SELECT * FROM executor_work_items
            WHERE run_id = ? AND work_id = ?
            """,
            (run_id, work_id),
        ).fetchone()
        if row is None:
            raise WorkItemNotFound(f"work_item_not_found: {work_id}")
        dependencies = tuple(
            str(item["depends_on_work_id"])
            for item in connection.execute(
                """
                SELECT depends_on_work_id FROM executor_dependencies
                WHERE run_id = ? AND work_id = ?
                ORDER BY ordinal, depends_on_work_id
                """,
                (run_id, work_id),
            ).fetchall()
        )
        barriers = tuple(
            str(item["barrier_id"])
            for item in connection.execute(
                """
                SELECT barrier_id FROM executor_barrier_releases
                WHERE run_id = ? AND work_id = ?
                ORDER BY ordinal, barrier_id
                """,
                (run_id, work_id),
            ).fetchall()
        )
        if ready is None:
            ready = self._is_ready(connection, run_id, work_id)
        return WorkItem(
            run_id=run_id,
            work_id=str(row["work_id"]),
            work_key=str(row["work_key"]),
            ordinal=int(row["ordinal"]),
            worker=str(row["worker"]),
            harness=str(row["harness"]),
            payload=json.loads(str(row["payload_json"])),
            state=str(row["state"]),
            available_at=float(row["available_at"]),
            claim_id=None if row["claim_id"] is None else str(row["claim_id"]),
            replica_slot=(
                None if row["replica_slot"] is None else str(row["replica_slot"])
            ),
            agent_name=None if row["agent_name"] is None else str(row["agent_name"]),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            dependencies=dependencies,
            barriers=barriers,
            ready=bool(ready),
        )

    def _barrier_record(self, connection: Any, row: Any) -> BarrierRecord:
        run_id = str(row["run_id"])
        barrier_id = str(row["barrier_id"])
        required = tuple(
            str(item["work_id"])
            for item in connection.execute(
                """
                SELECT work_id FROM executor_barrier_members
                WHERE run_id = ? AND barrier_id = ?
                ORDER BY ordinal, work_id
                """,
                (run_id, barrier_id),
            ).fetchall()
        )
        releases = tuple(
            str(item["work_id"])
            for item in connection.execute(
                """
                SELECT work_id FROM executor_barrier_releases
                WHERE run_id = ? AND barrier_id = ?
                ORDER BY ordinal, work_id
                """,
                (run_id, barrier_id),
            ).fetchall()
        )
        return BarrierRecord(
            run_id=run_id,
            barrier_id=barrier_id,
            state=str(row["state"]),
            ordinal=int(row["ordinal"]),
            required_work_ids=required,
            release_work_ids=releases,
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def _slot_record(self, row: Any) -> ReplicaSlot:
        return ReplicaSlot(
            workflow=str(row["workflow"]),
            harness=str(row["harness"]),
            slot_name=str(row["slot_name"]),
            slot_ordinal=int(row["slot_ordinal"]),
            active_run_id=(
                None if row["active_run_id"] is None else str(row["active_run_id"])
            ),
            active_work_id=(
                None if row["active_work_id"] is None else str(row["active_work_id"])
            ),
            claim_id=None if row["claim_id"] is None else str(row["claim_id"]),
            agent_name=None if row["agent_name"] is None else str(row["agent_name"]),
            updated_at=float(row["updated_at"]),
        )

    def _add_barrier_edge(
        self,
        run_id: str,
        barrier_id: str,
        work_id: str,
        *,
        member: bool,
        ordinal: int | None,
    ) -> BarrierRecord:
        run_id = _identifier(run_id, "run_id")
        barrier_id = _identifier(barrier_id, "barrier_id")
        work_id = _identifier(work_id, "work_id")
        if ordinal is not None and (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 0
        ):
            raise DependencyError("barrier_edge_ordinal_invalid")
        with self.store._transaction() as connection:
            self._require_run(connection, run_id)
            self._require_barrier(connection, run_id, barrier_id)
            work_id = self._resolve_work_id(connection, run_id, work_id)
            table = "executor_barrier_members" if member else "executor_barrier_releases"
            existing = connection.execute(
                f"""
                SELECT 1 FROM {table}
                WHERE run_id = ? AND barrier_id = ? AND work_id = ?
                """,
                (run_id, barrier_id, work_id),
            ).fetchone()
            if existing is None:
                barrier = connection.execute(
                    """
                    SELECT state FROM executor_barriers
                    WHERE run_id = ? AND barrier_id = ?
                    """,
                    (run_id, barrier_id),
                ).fetchone()
                if barrier is None:
                    raise DependencyError(f"barrier_not_found: {barrier_id}")
                if str(barrier["state"]) != "pending":
                    raise DependencyError("barrier_already_settled")
                other_table = (
                    "executor_barrier_releases"
                    if member
                    else "executor_barrier_members"
                )
                if connection.execute(
                    f"""
                    SELECT 1 FROM {other_table}
                    WHERE run_id = ? AND barrier_id = ? AND work_id = ?
                    """,
                    (run_id, barrier_id, work_id),
                ).fetchone() is not None:
                    raise DependencyError("barrier_member_release_overlap")
                if member:
                    release_ids = connection.execute(
                        """
                        SELECT work_id FROM executor_barrier_releases
                        WHERE run_id = ? AND barrier_id = ?
                        """,
                        (run_id, barrier_id),
                    ).fetchall()
                    pairs = (
                        (work_id, str(row["work_id"]))
                        for row in release_ids
                    )
                else:
                    member_ids = connection.execute(
                        """
                        SELECT work_id FROM executor_barrier_members
                        WHERE run_id = ? AND barrier_id = ?
                        """,
                        (run_id, barrier_id),
                    ).fetchall()
                    pairs = (
                        (str(row["work_id"]), work_id)
                        for row in member_ids
                    )
                if any(
                    self._work_depends_on(connection, run_id, left, right)
                    for left, right in pairs
                ):
                    raise DependencyError("barrier_cycle")
                if ordinal is None:
                    row = connection.execute(
                        f"""
                        SELECT COALESCE(MAX(ordinal) + 1, 0) AS next_ordinal
                        FROM {table} WHERE run_id = ? AND barrier_id = ?
                        """,
                        (run_id, barrier_id),
                    ).fetchone()
                    ordinal = int(row["next_ordinal"]) if row else 0
                connection.execute(
                    f"""
                    INSERT INTO {table}(run_id, barrier_id, work_id, ordinal)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, barrier_id, work_id, ordinal),
                )
                self._refresh_readiness(connection, run_id)
            row = connection.execute(
                """
                SELECT * FROM executor_barriers
                WHERE run_id = ? AND barrier_id = ?
                """,
                (run_id, barrier_id),
            ).fetchone()
            if row is None:
                raise KernelError("barrier_not_found")
            return self._barrier_record(connection, row)

    @staticmethod
    def _require_run(connection: Any, run_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM executor_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"{RunNotFoundError.code}: {run_id}")
        return row

    @staticmethod
    def _require_work(connection: Any, run_id: str, work_id: str) -> Any:
        row = connection.execute(
            """
            SELECT * FROM executor_work_items
            WHERE run_id = ? AND work_id = ?
            """,
            (run_id, work_id),
        ).fetchone()
        if row is None:
            raise WorkItemNotFound(f"work_item_not_found: {work_id}")
        return row

    @staticmethod
    def _require_barrier(connection: Any, run_id: str, barrier_id: str) -> Any:
        row = connection.execute(
            """
            SELECT * FROM executor_barriers
            WHERE run_id = ? AND barrier_id = ?
            """,
            (run_id, barrier_id),
        ).fetchone()
        if row is None:
            raise DependencyError(f"barrier_not_found: {barrier_id}")
        return row

    @staticmethod
    def _require_new_work_identity(
        connection: Any,
        run_id: str,
        work_id: str,
        work_key: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT work_id, work_key FROM executor_work_items
            WHERE run_id = ? AND (work_id = ? OR work_key = ?)
            """,
            (run_id, work_id, work_key),
        ).fetchone()
        if row is not None:
            raise WorkItemConflict(
                f"{WorkItemConflict.code}: run_id={run_id} "
                f"work_id={row['work_id']} work_key={row['work_key']}"
            )

    @staticmethod
    def _same_work_definition(
        connection: Any,
        row: Any,
        *,
        worker: str,
        harness: str,
        payload_json: str,
        dependencies: Sequence[str],
        barriers: Sequence[str],
        ordinal: int | None,
        available_at: float | None,
    ) -> bool:
        if (
            str(row["worker"]) != worker
            or str(row["harness"]) != harness
            or str(row["payload_json"]) != payload_json
        ):
            return False
        if ordinal is not None and int(row["ordinal"]) != ordinal:
            return False
        if available_at is not None and float(row["available_at"]) != available_at:
            return False
        actual_dependencies = tuple(
            str(item["depends_on_work_id"])
            for item in connection.execute(
                """
                SELECT depends_on_work_id FROM executor_dependencies
                WHERE run_id = ? AND work_id = ?
                ORDER BY ordinal, depends_on_work_id
                """,
                (str(row["run_id"]), str(row["work_id"])),
            ).fetchall()
        )
        actual_barriers = tuple(
            str(item["barrier_id"])
            for item in connection.execute(
                """
                SELECT barrier_id FROM executor_barrier_releases
                WHERE run_id = ? AND work_id = ?
                ORDER BY ordinal, barrier_id
                """,
                (str(row["run_id"]), str(row["work_id"])),
            ).fetchall()
        )
        return actual_dependencies == tuple(dependencies) and actual_barriers == tuple(barriers)

    @staticmethod
    def _resolve_work_id(connection: Any, run_id: str, value: str) -> str:
        row = connection.execute(
            """
            SELECT work_id FROM executor_work_items
            WHERE run_id = ? AND (work_id = ? OR work_key = ?)
            ORDER BY work_id
            """,
            (run_id, value, value),
        ).fetchall()
        if not row:
            raise WorkItemNotFound(f"work_item_not_found: {value}")
        if len(row) > 1:
            raise DependencyError(f"work_item_reference_ambiguous: {value}")
        return str(row[0]["work_id"])

    def _would_cycle(
        self,
        connection: Any,
        run_id: str,
        work_id: str,
        dependencies: Sequence[str],
        *,
        pending_items: Sequence[Mapping[str, Any]] = (),
    ) -> bool:
        """Return whether adding edges would make a finite dependency cycle."""

        edges: dict[str, set[str]] = {}
        for row in connection.execute(
            """
            SELECT work_id, depends_on_work_id
            FROM executor_dependencies WHERE run_id = ?
            """,
            (run_id,),
        ).fetchall():
            edges.setdefault(str(row["work_id"]), set()).add(
                str(row["depends_on_work_id"])
            )
        for item in pending_items:
            item_id = str(item["work_id"])
            edges.setdefault(item_id, set()).update(
                self._resolve_pending_reference(
                    connection,
                    run_id,
                    str(value),
                    pending_items,
                )
                for value in item.get("depends_on", ())
            )
        resolved: set[str] = set()
        for dependency in dependencies:
            resolved.add(
                self._resolve_pending_reference(
                    connection,
                    run_id,
                    dependency,
                    pending_items,
                )
            )
        edges.setdefault(work_id, set()).update(resolved)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            for parent in edges.get(node, ()):
                if visit(parent):
                    return True
            visiting.remove(node)
            visited.add(node)
            return False

        return visit(work_id)

    @staticmethod
    def _work_depends_on(
        connection: Any,
        run_id: str,
        work_id: str,
        target_work_id: str,
    ) -> bool:
        pending = [work_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            if current == target_work_id:
                return True
            rows = connection.execute(
                """
                SELECT depends_on_work_id FROM executor_dependencies
                WHERE run_id = ? AND work_id = ?
                """,
                (run_id, current),
            ).fetchall()
            pending.extend(str(row["depends_on_work_id"]) for row in rows)
        return False

    @staticmethod
    def _resolve_pending_reference(
        connection: Any,
        run_id: str,
        value: str,
        pending_items: Sequence[Mapping[str, Any]],
    ) -> str:
        matches = [
            str(item["work_id"])
            for item in pending_items
            if value in {str(item["work_id"]), str(item["work_key"])}
        ]
        existing = connection.execute(
            """
            SELECT work_id FROM executor_work_items
            WHERE run_id = ? AND (work_id = ? OR work_key = ?)
            """,
            (run_id, value, value),
        ).fetchall()
        matches.extend(str(row["work_id"]) for row in existing)
        unique = sorted(set(matches))
        if not unique:
            raise WorkItemNotFound(f"work_item_not_found: {value}")
        if len(unique) > 1:
            raise DependencyError(f"work_item_reference_ambiguous: {value}")
        return unique[0]

    def _ensure_slots(
        self,
        connection: Any,
        workflow: str,
        harness: str,
        worker: str | None,
        now: float,
        names: Sequence[str] | None,
    ) -> tuple[str, ...]:
        existing_rows = connection.execute(
            """
            SELECT slot_name FROM executor_replica_slots
            WHERE workflow = ? AND harness = ?
            ORDER BY slot_ordinal, slot_name
            """,
            (workflow, harness),
        ).fetchall()
        existing = tuple(str(row["slot_name"]) for row in existing_rows)
        if existing:
            configured = self._configured_slot_names(workflow, harness, worker)
            if configured is not None and existing != configured:
                raise ReplicaCapacityError("replica_capacity_conflict")
            return existing
        if names is None:
            capacity = self._capacity_for(harness, worker)
            names = self._replica_slots.get(
                worker or "",
                self._replica_slots.get(
                    harness,
                    self._generated_slot_names(workflow, harness, capacity),
                ),
            )
        names = tuple(names)
        if not names:
            raise ReplicaCapacityError("replica_slots_empty")
        for ordinal, slot_name in enumerate(names):
            connection.execute(
                """
                INSERT INTO executor_replica_slots(
                    workflow, harness, slot_name, slot_ordinal,
                    active_run_id, active_work_id, claim_id, agent_name, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
                """,
                (workflow, harness, slot_name, ordinal, now),
            )
        return names

    def _configured_slot_names(
        self,
        workflow: str,
        harness: str,
        worker: str | None,
    ) -> tuple[str, ...] | None:
        configured = None
        if worker is not None and worker in self._replica_slots:
            configured = self._replica_slots[worker]
        elif harness in self._replica_slots:
            configured = self._replica_slots[harness]
        elif worker is not None and worker in self._replica_capacity:
            configured = self._generated_slot_names(
                workflow,
                harness,
                self._replica_capacity[worker],
            )
        elif harness in self._replica_capacity:
            configured = self._generated_slot_names(
                workflow,
                harness,
                self._replica_capacity[harness],
            )
        return configured

    def _capacity_for(self, harness: str, worker: str | None) -> int:
        if worker is not None and worker in self._replica_capacity:
            return self._replica_capacity[worker]
        if harness in self._replica_capacity:
            return self._replica_capacity[harness]
        return 1

    def _generated_slot_names(
        self,
        workflow: str,
        harness: str,
        capacity: int,
    ) -> tuple[str, ...]:
        workspace = "" if self.workspace is None else str(self.workspace)
        seed = f"{workflow}\0{workspace}\0{harness}"
        if capacity == 1:
            digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
            return (f"ho-{harness}-{digest}",)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6]
        return tuple(
            f"ho-{harness}-{index:02d}-{digest}"
            for index in range(1, capacity + 1)
        )


def _identifier(value: Any, field: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise KernelError(f"{field}_must_be_non_empty_string")
    normalized = value.strip()
    if len(normalized) > MAX_ID_LENGTH:
        raise KernelError(f"{field}_too_long")
    if normalized != value:
        raise KernelError(f"{field}_must_be_trimmed")
    return normalized


def _identifier_sequence(value: Iterable[str], field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or value is None:
        raise KernelError(f"{field}_must_be_string_array")
    try:
        values = tuple(_identifier(item, field) for item in value)
    except TypeError as exc:
        raise KernelError(f"{field}_must_be_string_array") from exc
    if len(values) > MAX_WORK_ITEMS_PER_RUN:
        raise KernelError(f"{field}_too_many_items")
    if len(set(values)) != len(values):
        raise KernelError(f"{field}_duplicate")
    return values


def _validate_limit(value: int | None, maximum: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise KernelError(f"claim_limit_must_be_integer_1_{maximum}")
    return value


def _validate_capacity_mapping(
    value: Mapping[str, int] | None,
) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReplicaCapacityError("replica_capacity_must_be_mapping")
    result: dict[str, int] = {}
    for key, capacity in value.items():
        normalized_key = _identifier(key, "replica_capacity_key")
        if (
            not isinstance(capacity, int)
            or isinstance(capacity, bool)
            or not 1 <= capacity <= MAX_REPLICA_SLOTS
        ):
            raise ReplicaCapacityError(
                f"replica_capacity_out_of_range: {normalized_key}"
            )
        result[normalized_key] = capacity
    return result


def _validate_slot_mapping(
    value: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReplicaCapacityError("replica_slots_must_be_mapping")
    result: dict[str, tuple[str, ...]] = {}
    for key, names in value.items():
        normalized_key = _identifier(key, "replica_slots_key")
        if isinstance(names, (str, bytes)):
            raise ReplicaCapacityError("replica_slots_must_be_string_arrays")
        try:
            normalized_names = _identifier_sequence(names, "replica_slot")
        except (TypeError, KernelError) as exc:
            raise ReplicaCapacityError(str(exc)) from exc
        if not normalized_names or len(normalized_names) > MAX_REPLICA_SLOTS:
            raise ReplicaCapacityError(
                f"replica_slot_count_out_of_range: {normalized_key}"
            )
        result[normalized_key] = normalized_names
    return result


def _prepare_work_mapping(item: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise WorkItemConflict("work_item_must_be_mapping")
    allowed = {
        "work_id",
        "id",
        "work_key",
        "key",
        "ordinal",
        "worker",
        "harness",
        "payload",
        "depends_on",
        "dependencies",
        "barrier_ids",
        "depends_on_barriers",
        "available_at",
    }
    unknown = set(item) - allowed
    if unknown:
        raise WorkItemConflict(f"work_item_unknown_field: {sorted(unknown)[0]}")
    if "work_id" in item and "id" in item:
        raise WorkItemConflict("work_item_duplicate_id_argument")
    if "work_key" in item and "key" in item:
        raise WorkItemConflict("work_item_duplicate_key_argument")
    return dict(item)
