from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from herdr_orchestrator.executor_protocol import (
    CANONICALIZATION_VERSION,
    DEFINITION_CANONICALIZATION_VERSION,
    EMPTY_DIGEST,
    MANIFEST_DIGEST_FIELDS,
    MANIFEST_VERSION,
    PINNED_MANIFEST_VERSION,
    DefinitionIdentity,
    DefinitionError,
    ManifestValidationError,
    PinnedManifest,
    PinnedRunManifest,
    RunManifest,
    canonical_definition,
    canonical_json,
    canonicalize_definition,
    definition_digest,
    digest_definition,
    hash_definition,
    normalize_definition,
    normalized_definition_identity,
    run_identity_digest,
    UNSET as _UNSET,
)


__all__ = [
    "CANONICALIZATION_VERSION",
    "DEFINITION_CANONICALIZATION_VERSION",
    "EMPTY_DIGEST",
    "EXECUTOR_SCHEMA_VERSION",
    "ExecutorStore",
    "ExecutorStoreError",
    "DefinitionError",
    "DefinitionIdentity",
    "ManifestValidationError",
    "MANIFEST_DIGEST_FIELDS",
    "MANIFEST_VERSION",
    "PINNED_MANIFEST_VERSION",
    "PinnedManifest",
    "PinnedRunManifest",
    "RunDedupeConflict",
    "RunManifest",
    "RunNotFoundError",
    "RunRecord",
    "RUN_STORE_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "canonical_definition",
    "canonical_json",
    "canonicalize_definition",
    "definition_digest",
    "digest_definition",
    "hash_definition",
    "normalize_definition",
    "normalized_definition_identity",
    "run_identity_digest",
]


SCHEMA_VERSION = 1
EXECUTOR_SCHEMA_VERSION = SCHEMA_VERSION
RUN_STORE_SCHEMA_VERSION = SCHEMA_VERSION
_RUN_STATES = frozenset(
    {
        "pending",
        "running",
        "succeeded",
        "blocked",
        "failed",
        "cancelled",
        "paused",
    }
)


class ExecutorStoreError(RuntimeError):
    """Base error for schema-v2 persistence failures."""

    code = "executor_store_error"


class RunNotFoundError(ExecutorStoreError):
    code = "run_not_found"


class RunDedupeConflict(ExecutorStoreError):
    code = "run_dedupe_conflict"

    def __init__(self, workflow: str, dedupe_key: str, existing_run_id: str) -> None:
        self.workflow = workflow
        self.dedupe_key = dedupe_key
        self.existing_run_id = existing_run_id
        super().__init__(
            f"{self.code}: workflow={workflow} dedupe_key={dedupe_key} "
            f"existing_run_id={existing_run_id}"
        )


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    workflow: str
    executor_kind: str
    dedupe_key: str
    state: str
    manifest: PinnedRunManifest
    created_at: float
    updated_at: float

    @property
    def id(self) -> str:
        return self.run_id

    @property
    def workflow_name(self) -> str:
        return self.workflow

    @property
    def pinned_manifest(self) -> PinnedRunManifest:
        return self.manifest

    @property
    def identity_digest(self) -> str:
        return run_identity_digest(self.workflow, self.executor_kind, self.manifest)

    @property
    def manifest_digest(self) -> str:
        return self.manifest.manifest_digest

    def to_dict(self) -> dict[str, object]:
        manifest = self.manifest.to_dict()
        return {
            "schema_version": 2,
            "run_id": self.run_id,
            "workflow": self.workflow,
            "executor": self.executor_kind,
            "dedupe_key": self.dedupe_key,
            "state": self.state,
            "manifest": manifest,
            "pinned_manifest": manifest,
            "manifest_digest": self.manifest_digest,
            "identity_digest": self.identity_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ExecutorStore:
    """Additive schema-v2 run storage.

    This store deliberately never creates, updates, or reads the schema-v1
    queue tables. A database may therefore be shared by legacy and v2 callers
    without copying or reinterpreting legacy rows.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS executor_schema_meta (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL CHECK (version >= 1)
                )
                """
            )
            row = connection.execute(
                "SELECT version FROM executor_schema_meta WHERE component = ?",
                ("run-store",),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO executor_schema_meta(component, version) VALUES (?, ?)",
                    ("run-store", SCHEMA_VERSION),
                )
            elif int(row["version"]) != SCHEMA_VERSION:
                raise ExecutorStoreError(
                    f"unsupported_executor_schema_version: {row['version']}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS executor_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    executor_kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'pending', 'running', 'succeeded', 'blocked',
                            'failed', 'cancelled', 'paused'
                        )
                    ),
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    identity_digest TEXT NOT NULL,
                    workflow_digest TEXT NOT NULL,
                    config_digest TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    route_digest TEXT NOT NULL,
                    profile_digest TEXT NOT NULL,
                    prompt_digest TEXT NOT NULL,
                    static_check_digest TEXT NOT NULL,
                    contract_digest TEXT NOT NULL,
                    executor_digest TEXT NOT NULL,
                    artifact_contract_digest TEXT NOT NULL,
                    canonicalization_version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(workflow, dedupe_key)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS executor_runs_state_order
                ON executor_runs(workflow, state, created_at, run_id)
                """
            )

    def feature_version(self) -> int:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT version FROM executor_schema_meta WHERE component = ?",
                ("run-store",),
            ).fetchone()
        if row is None:
            raise ExecutorStoreError("executor_schema_version_missing")
        return int(row["version"])

    def create_run(
        self,
        workflow: str,
        executor_kind: str | Enum,
        dedupe_key: str,
        manifest: PinnedRunManifest | Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
        state: str = "pending",
        workflow_definition: Any = _UNSET,
        config_definition: Any = _UNSET,
        input_value: Any = _UNSET,
        source_definition: Any = _UNSET,
        route_definition: Any = _UNSET,
        profile_definition: Any = _UNSET,
        prompt_definition: Any = _UNSET,
        static_checks: Any = _UNSET,
        contract_definition: Any = _UNSET,
        executor_definition: Any = _UNSET,
        artifact_contract_definition: Any = _UNSET,
        **digest_overrides: Any,
    ) -> tuple[str, bool]:
        """Create or return a run for an explicit dedupe key.

        A matching key and matching pinned identity returns the existing run.
        The same key with a different identity raises ``RunDedupeConflict``
        before changing the existing row.
        """

        workflow = _required_text(workflow, "workflow")
        executor_value = _required_text(_enum_value(executor_kind), "executor_kind")
        dedupe_key = _required_text(dedupe_key, "dedupe_key")
        if state not in _RUN_STATES:
            raise ExecutorStoreError(f"run_state_invalid: {state}")
        pinned = self._manifest(
            workflow=workflow,
            manifest=manifest,
            workflow_definition=workflow_definition,
            config_definition=config_definition,
            input_value=input_value,
            source_definition=source_definition,
            route_definition=route_definition,
            profile_definition=profile_definition,
            prompt_definition=prompt_definition,
            static_checks=static_checks,
            contract_definition=contract_definition,
            executor_definition=executor_definition,
            artifact_contract_definition=artifact_contract_definition,
            digest_overrides=digest_overrides,
        )
        if run_id is not None:
            run_id = _required_text(run_id, "run_id")

        self.initialize()
        identity_digest = run_identity_digest(workflow, executor_value, pinned)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT run_id, executor_kind, identity_digest
                FROM executor_runs
                WHERE workflow = ? AND dedupe_key = ?
                """,
                (workflow, dedupe_key),
            ).fetchone()
            if existing is not None:
                same_identity = (
                    str(existing["executor_kind"]) == executor_value
                    and str(existing["identity_digest"]) == identity_digest
                )
                existing_id = str(existing["run_id"])
                if same_identity:
                    return existing_id, False
                raise RunDedupeConflict(workflow, dedupe_key, existing_id)

            actual_id = run_id or self._new_run_id(connection)
            now = time.time()
            connection.execute(
                """
                INSERT INTO executor_runs(
                    run_id, workflow, executor_kind, dedupe_key, state,
                    manifest_json, manifest_digest, identity_digest,
                    workflow_digest, config_digest, input_digest, source_digest,
                    route_digest, profile_digest, prompt_digest, static_check_digest,
                    contract_digest, executor_digest, artifact_contract_digest,
                    canonicalization_version, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actual_id,
                    workflow,
                    executor_value,
                    dedupe_key,
                    state,
                    pinned.to_json(),
                    pinned.manifest_digest,
                    identity_digest,
                    pinned.workflow_digest,
                    pinned.config_digest,
                    pinned.input_digest,
                    pinned.source_digest,
                    pinned.route_digest,
                    pinned.profile_digest,
                    pinned.prompt_digest,
                    pinned.static_check_digest,
                    pinned.contract_digest,
                    pinned.executor_digest,
                    pinned.artifact_contract_digest,
                    pinned.canonicalization_version,
                    now,
                    now,
                ),
            )
            return actual_id, True

    def create_run_record(
        self,
        workflow: str,
        executor_kind: str | Enum,
        dedupe_key: str,
        manifest: PinnedRunManifest | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[RunRecord, bool]:
        run_id, created = self.create_run(
            workflow,
            executor_kind,
            dedupe_key,
            manifest,
            **kwargs,
        )
        return self.require_run(run_id), created

    # Explicit aliases keep the persistence seam readable to executor code.
    get_or_create_run = create_run
    start_run = create_run

    def get_run(self, run_id: str) -> RunRecord | None:
        run_id = _required_text(run_id, "run_id")
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM executor_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else self._record(row)

    def require_run(self, run_id: str) -> RunRecord:
        record = self.get_run(run_id)
        if record is None:
            raise RunNotFoundError(f"{RunNotFoundError.code}: {run_id}")
        return record

    def find_run(self, workflow: str, dedupe_key: str) -> RunRecord | None:
        workflow = _required_text(workflow, "workflow")
        dedupe_key = _required_text(dedupe_key, "dedupe_key")
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM executor_runs
                WHERE workflow = ? AND dedupe_key = ?
                """,
                (workflow, dedupe_key),
            ).fetchone()
        return None if row is None else self._record(row)

    def runs(self, workflow: str | None = None) -> list[RunRecord]:
        self.initialize()
        with self._connect() as connection:
            if workflow is None:
                rows = connection.execute(
                    "SELECT * FROM executor_runs ORDER BY created_at, run_id"
                ).fetchall()
            else:
                workflow = _required_text(workflow, "workflow")
                rows = connection.execute(
                    """
                    SELECT * FROM executor_runs
                    WHERE workflow = ?
                    ORDER BY created_at, run_id
                    """,
                    (workflow,),
                ).fetchall()
        return [self._record(row) for row in rows]

    list_runs = runs

    def get_manifest(self, run_id: str) -> PinnedRunManifest:
        return self.require_run(run_id).manifest

    pinned_manifest = get_manifest

    def inspect_run(self, run_id: str) -> dict[str, object]:
        """Return the durable run record for higher-level inspect surfaces."""

        return self.require_run(run_id).to_dict()

    inspect = inspect_run

    def _manifest(
        self,
        *,
        workflow: str,
        manifest: PinnedRunManifest | Mapping[str, Any] | None,
        workflow_definition: Any,
        config_definition: Any,
        input_value: Any,
        source_definition: Any,
        route_definition: Any,
        profile_definition: Any,
        prompt_definition: Any,
        static_checks: Any,
        contract_definition: Any,
        executor_definition: Any,
        artifact_contract_definition: Any,
        digest_overrides: Mapping[str, Any],
    ) -> PinnedRunManifest:
        definitions = (
            workflow_definition,
            config_definition,
            input_value,
            source_definition,
            route_definition,
            profile_definition,
            prompt_definition,
            static_checks,
            contract_definition,
            executor_definition,
            artifact_contract_definition,
        )
        if manifest is not None:
            if any(value is not _UNSET for value in definitions) or digest_overrides:
                raise ManifestValidationError(
                    f"{ManifestValidationError.code}: mixed_manifest_inputs"
                )
            if isinstance(manifest, PinnedRunManifest):
                manifest.validate()
                return manifest
            return PinnedRunManifest.from_mapping(manifest)

        overrides = dict(digest_overrides)
        aliases = {
            "workflow_digest": "workflow_digest",
            "config_digest": "config_digest",
            "input_digest": "input_digest",
            "source_digest": "source_digest",
            "route_digest": "route_digest",
            "profile_digest": "profile_digest",
            "prompt_digest": "prompt_digest",
            "prompt_rubric_digest": "prompt_digest",
            "static_check_digest": "static_check_digest",
            "contract_digest": "contract_digest",
            "executor_contract_digest": "contract_digest",
            "artifact_digest": "artifact_contract_digest",
            "artifact_contract_digest": "artifact_contract_digest",
            "executor_implementation_digest": "executor_digest",
            "executor_digest": "executor_digest",
        }
        unknown = set(overrides) - set(aliases)
        if unknown:
            raise ManifestValidationError(
                f"{ManifestValidationError.code}: unknown_digest:{sorted(unknown)[0]}"
            )
        for key, target in aliases.items():
            if key in overrides:
                if target in overrides:
                    raise ManifestValidationError(
                        f"{ManifestValidationError.code}: duplicate_digest:{target}"
                    )
                overrides[target] = overrides.pop(key)
        return PinnedRunManifest.from_values(
            workflow_name=workflow,
            workflow=(
                _UNSET
                if workflow_definition is _UNSET
                else workflow_definition
            ),
            config=None if config_definition is _UNSET else config_definition,
            input_value=None if input_value is _UNSET else input_value,
            source=None if source_definition is _UNSET else source_definition,
            route=None if route_definition is _UNSET else route_definition,
            profile=None if profile_definition is _UNSET else profile_definition,
            prompt=None if prompt_definition is _UNSET else prompt_definition,
            static_checks=None if static_checks is _UNSET else static_checks,
            contract=None if contract_definition is _UNSET else contract_definition,
            executor=None if executor_definition is _UNSET else executor_definition,
            artifact_contract=None
            if artifact_contract_definition is _UNSET
            else artifact_contract_definition,
            **overrides,
        )

    @staticmethod
    def _new_run_id(connection: sqlite3.Connection) -> str:
        for _ in range(8):
            candidate = f"run_{uuid.uuid4().hex}"
            if connection.execute(
                "SELECT 1 FROM executor_runs WHERE run_id = ?",
                (candidate,),
            ).fetchone() is None:
                return candidate
        raise ExecutorStoreError("run_id_generation_exhausted")

    @staticmethod
    def _record(row: sqlite3.Row) -> RunRecord:
        manifest = PinnedRunManifest.from_json(str(row["manifest_json"]))
        if (
            str(row["manifest_json"]) != manifest.to_json()
            or manifest.manifest_digest != str(row["manifest_digest"])
        ):
            raise ExecutorStoreError("run_manifest_digest_mismatch")
        record = RunRecord(
            run_id=str(row["run_id"]),
            workflow=str(row["workflow"]),
            executor_kind=str(row["executor_kind"]),
            dedupe_key=str(row["dedupe_key"]),
            state=str(row["state"]),
            manifest=manifest,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
        if record.identity_digest != str(row["identity_digest"]):
            raise ExecutorStoreError("run_identity_digest_mismatch")
        for field in MANIFEST_DIGEST_FIELDS:
            if str(row[field]) != getattr(record.manifest, field):
                raise ExecutorStoreError(f"run_{field}_mismatch")
        return record

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()


def _enum_value(value: str | Enum) -> str:
    return str(value.value if isinstance(value, Enum) else value)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutorStoreError(f"{field}_must_be_non_empty_string")
    return value.strip()
