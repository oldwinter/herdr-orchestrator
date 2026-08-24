from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from herdr_orchestrator.executor_protocol import MANIFEST_DIGEST_FIELDS, canonical_json


ARTIFACT_CONTRACT_VERSION = 1
ARTIFACT_SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 1_000_000
MAX_ARTIFACT_PATH_LENGTH = 256
MAX_ARTIFACTS_PER_ATTEMPT = 32
_DIGEST_PREFIX = "sha256:"

__all__ = [
    "ARTIFACT_CONTRACT_VERSION",
    "ARTIFACT_SCHEMA_VERSION",
    "ArtifactEnvelope",
    "ArtifactError",
    "ArtifactPathError",
    "ArtifactRecord",
    "ArtifactStore",
    "ArtifactValidationError",
    "AttemptRoots",
    "MAX_ARTIFACT_BYTES",
    "MAX_ARTIFACT_PATH_LENGTH",
    "MAX_ARTIFACTS_PER_ATTEMPT",
    "PreparedArtifact",
    "atomic_write_bytes",
    "digest_bytes",
    "digest_file",
]


class ArtifactError(ValueError):
    """Base error for typed artifact staging and admission."""

    code = "artifact_error"


class ArtifactValidationError(ArtifactError):
    """Raised when an artifact envelope or its content fails closed."""

    code = "artifact_invalid"


class ArtifactPathError(ArtifactValidationError):
    code = "artifact_path_invalid"


@dataclass(frozen=True, slots=True)
class AttemptRoots:
    """Exclusive, attempt-owned runtime directories.

    ``out`` is reserved for assigned final output files.  ``scratch`` is
    intentionally separate so temporary material is never mistaken for
    admitted workflow evidence.
    """

    root: Path
    out: Path
    scratch: Path
    boundary: Path | None = None

    @classmethod
    def for_claim(cls, runtime_dir: Path | str, claim: Any) -> AttemptRoots:
        runtime = Path(runtime_dir).expanduser().resolve()
        run_id = _identifier(getattr(claim, "run_id", None), "run_id")
        work_id = _identifier(getattr(claim, "work_id", None), "work_id")
        attempt_number = getattr(claim, "attempt_number", None)
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number < 1
        ):
            raise ArtifactValidationError("attempt_number_invalid")
        fencing_token = _identifier(
            getattr(claim, "fencing_token", None),
            "fencing_token",
        )
        root = (
            runtime
            / run_id
            / "attempts"
            / work_id
            / f"{attempt_number}-{fencing_token}"
        )
        return cls(
            root=root,
            out=root / "out",
            scratch=root / "scratch",
            boundary=runtime,
        )

    @property
    def out_dir(self) -> Path:
        return self.out

    @property
    def scratch_dir(self) -> Path:
        return self.scratch

    def ensure(self) -> AttemptRoots:
        self._assert_root_shape()
        self._assert_no_symlink_components()
        if self.root.exists() and self.root.is_symlink():
            raise ArtifactPathError("attempt_root_symlink")
        self.out.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        if not self.out.is_dir() or self.out.is_symlink():
            raise ArtifactPathError("attempt_out_invalid")
        if not self.scratch.is_dir() or self.scratch.is_symlink():
            raise ArtifactPathError("attempt_scratch_invalid")
        self._assert_contained(self.out)
        self._assert_contained(self.scratch)
        self._assert_no_symlink_components()
        return self

    create = ensure

    def assigned_output(self, relative_path: str | Path) -> Path:
        """Resolve one assigned path without following an escaping symlink."""

        candidate = _relative_path(relative_path, field="artifact_path")
        output = self.out / candidate
        self._assert_contained(output)
        return output

    output_path = assigned_output
    assigned_path = assigned_output

    def scratch_path(self, relative_path: str | Path) -> Path:
        candidate = _relative_path(relative_path, field="scratch_path")
        path = self.scratch / candidate
        self._assert_contained(path)
        return path

    def cleanup(self) -> bool:
        """Remove only this attempt tree, never its runtime boundary."""

        self._assert_root_shape()
        if not self.root.exists() and not self.root.is_symlink():
            return False
        if self.root.is_symlink():
            raise ArtifactPathError("attempt_root_symlink")
        self._assert_no_symlink_components()
        resolved_root = self.root.resolve(strict=False)
        if resolved_root != self.root or (
            self.boundary is not None
            and not resolved_root.is_relative_to(self.boundary.resolve(strict=False))
        ):
            raise ArtifactPathError("attempt_root_escape")
        _remove_owned_tree(self.root)
        return True

    remove = cleanup

    def _assert_root_shape(self) -> None:
        if (
            self.root.name == ""
            or self.root.parent.name == ""
            or self.root.parent.parent.name != "attempts"
        ):
            raise ArtifactPathError("attempt_root_invalid")
        if self.out != self.root / "out" or self.scratch != self.root / "scratch":
            raise ArtifactPathError("attempt_root_layout_invalid")

    def _assert_contained(self, path: Path) -> None:
        resolved_root = self.root.resolve(strict=False)
        if self.root.exists() and self.root.is_symlink():
            raise ArtifactPathError("attempt_root_symlink")
        # Existing components are resolved to catch a symlinked directory,
        # while a not-yet-created final file remains lexically contained.
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(resolved_root):
            raise ArtifactPathError("artifact_path_escape")
        if self.boundary is not None and not resolved.is_relative_to(
            self.boundary.resolve(strict=False)
        ):
            raise ArtifactPathError("artifact_path_escape")

    def _assert_no_symlink_components(self) -> None:
        if self.boundary is None:
            return
        boundary = self.boundary.resolve(strict=False)
        try:
            relative = self.root.relative_to(boundary)
        except ValueError as exc:
            raise ArtifactPathError("attempt_root_escape") from exc
        current = boundary
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ArtifactPathError("attempt_root_symlink")


@dataclass(frozen=True, slots=True)
class ArtifactEnvelope:
    """Exact-key common envelope accepted at the kernel artifact boundary."""

    contract_version: int
    artifact_type: str
    run_id: str
    work_id: str
    logical_id: str
    attempt_id: str
    fencing_token: str
    path: str
    content_digest: str
    size_bytes: int
    lineage: tuple[str, ...]
    pinned_digests: Mapping[str, str]
    harness: str
    worker: str
    agent: str
    pane: str | None
    payload: Any

    EXACT_KEYS = frozenset(
        {
            "contract_version",
            "artifact_type",
            "run_id",
            "work_id",
            "logical_id",
            "attempt_id",
            "fencing_token",
            "path",
            "content_digest",
            "size_bytes",
            "lineage",
            "pinned_digests",
            "harness",
            "worker",
            "agent",
            "pane",
            "payload",
        }
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ArtifactEnvelope:
        if not isinstance(value, Mapping):
            raise ArtifactValidationError("artifact_envelope_object_required")
        unknown = set(value) - cls.EXACT_KEYS
        missing = cls.EXACT_KEYS - set(value)
        if unknown:
            raise ArtifactValidationError(
                f"artifact_unknown_field:{sorted(unknown, key=str)[0]}"
            )
        if missing:
            raise ArtifactValidationError(
                f"artifact_missing_field:{sorted(missing)[0]}"
            )
        contract_version = value["contract_version"]
        if (
            not isinstance(contract_version, int)
            or isinstance(contract_version, bool)
            or contract_version != ARTIFACT_CONTRACT_VERSION
        ):
            raise ArtifactValidationError("artifact_contract_version_invalid")
        artifact_type = _identifier(value["artifact_type"], "artifact_type")
        run_id = _identifier(value["run_id"], "run_id")
        work_id = _identifier(value["work_id"], "work_id")
        logical_id = _identifier(value["logical_id"], "logical_id")
        attempt_id = _identifier(value["attempt_id"], "attempt_id")
        fencing_token = _identifier(value["fencing_token"], "fencing_token")
        path = str(value["path"]) if isinstance(value["path"], str) else ""
        if not path:
            raise ArtifactValidationError("artifact_path_invalid")
        digest = _digest(value["content_digest"], "content_digest")
        size_bytes = value["size_bytes"]
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or size_bytes > MAX_ARTIFACT_BYTES
        ):
            raise ArtifactValidationError("artifact_size_invalid")
        lineage = value["lineage"]
        if (
            isinstance(lineage, (str, bytes))
            or not isinstance(lineage, (list, tuple))
            or not all(isinstance(item, str) and item for item in lineage)
            or not lineage
            or len(set(lineage)) != len(lineage)
        ):
            raise ArtifactValidationError("artifact_lineage_invalid")
        pinned = value["pinned_digests"]
        if not isinstance(pinned, Mapping):
            raise ArtifactValidationError("artifact_pinned_digests_invalid")
        pinned_copy: dict[str, str] = {}
        for key, pinned_digest in pinned.items():
            if not isinstance(key, str) or not key:
                raise ArtifactValidationError("artifact_pinned_digest_key_invalid")
            pinned_copy[key] = _digest(pinned_digest, f"pinned_digest:{key}")
        harness = _identifier(value["harness"], "harness")
        worker = _identifier(value["worker"], "worker")
        agent = _identifier(value["agent"], "agent")
        pane = value["pane"]
        pane = _identifier(pane, "pane")
        return cls(
            contract_version=contract_version,
            artifact_type=artifact_type,
            run_id=run_id,
            work_id=work_id,
            logical_id=logical_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            path=path,
            content_digest=digest,
            size_bytes=size_bytes,
            lineage=tuple(lineage),
            pinned_digests=pinned_copy,
            harness=harness,
            worker=worker,
            agent=agent,
            pane=pane,
            payload=value["payload"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "artifact_type": self.artifact_type,
            "run_id": self.run_id,
            "work_id": self.work_id,
            "logical_id": self.logical_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "path": self.path,
            "content_digest": self.content_digest,
            "size_bytes": self.size_bytes,
            "lineage": list(self.lineage),
            "pinned_digests": dict(self.pinned_digests),
            "harness": self.harness,
            "worker": self.worker,
            "agent": self.agent,
            "pane": self.pane,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    work_id: str
    attempt_id: str
    fencing_token: str
    artifact_type: str
    path: str
    content_digest: str
    size_bytes: int
    lineage: tuple[str, ...]
    pinned_digests: Mapping[str, str]
    harness: str
    worker: str
    agent: str
    pane: str | None
    state: str
    error_code: str | None
    envelope: Mapping[str, Any]
    admitted_path: str | None
    created_at: float

    @property
    def admitted(self) -> bool:
        return self.state == "admitted"

    @property
    def rejected(self) -> bool:
        return self.state in {"rejected", "stale"}

    @property
    def stale(self) -> bool:
        return self.state == "stale"

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "artifact_type": self.artifact_type,
            "path": self.path,
            "content_digest": self.content_digest,
            "size_bytes": self.size_bytes,
            "lineage": list(self.lineage),
            "pinned_digests": dict(self.pinned_digests),
            "harness": self.harness,
            "worker": self.worker,
            "agent": self.agent,
            "pane": self.pane,
            "state": self.state,
            "error_code": self.error_code,
            "envelope": dict(self.envelope),
            "admitted_path": self.admitted_path,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PreparedArtifact:
    envelope: ArtifactEnvelope
    source_path: Path
    admitted_path: Path
    artifact_id: str


class ArtifactStore:
    """Filesystem and durable-record adapter for common typed artifacts.

    Admitted bytes live in a content-addressed directory outside the attempt
    root.  Attempt cleanup can therefore remove unadmitted output and scratch
    without invalidating durable receipts or evidence.
    """

    def __init__(
        self,
        store: Any,
        runtime_dir: Path | str,
        *,
        clock: Any | None = None,
    ) -> None:
        self.store = store
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self._clock = clock

    @property
    def content_dir(self) -> Path:
        return self.runtime_dir / "artifacts"

    def initialize(self) -> None:
        self.store.initialize()

    def roots(self, claim: Any) -> AttemptRoots:
        return AttemptRoots.for_claim(self.runtime_dir, claim)

    attempt_roots = roots

    def cleanup_attempt(self, claim: Any) -> bool:
        return self.roots(claim).cleanup()

    cleanup = cleanup_attempt

    def stage(
        self,
        claim: Any,
        relative_path: str | Path,
        content: bytes,
    ) -> tuple[Path, str, int]:
        self.initialize()
        roots = self.roots(claim).ensure()
        destination = roots.assigned_output(relative_path)
        digest, size = atomic_write_bytes(destination, content)
        return destination, digest, size

    write_output = stage
    stage_output = stage

    def stage_scratch(
        self,
        claim: Any,
        relative_path: str | Path,
        content: bytes,
    ) -> tuple[Path, str, int]:
        self.initialize()
        roots = self.roots(claim).ensure()
        destination = roots.scratch_path(relative_path)
        digest, size = atomic_write_bytes(destination, content)
        return destination, digest, size

    write_scratch = stage_scratch

    def prepare(
        self,
        claim: Any,
        envelope: ArtifactEnvelope | Mapping[str, Any],
        *,
        expected_lineage: tuple[str, ...] | list[str] | None = None,
        expected_path: str | Path | None = None,
    ) -> PreparedArtifact:
        self.initialize()
        parsed = (
            envelope
            if isinstance(envelope, ArtifactEnvelope)
            else ArtifactEnvelope.from_mapping(envelope)
        )
        _validate_envelope_against_claim(parsed, claim)
        roots = self.roots(claim).ensure()
        self._enforce_attempt_artifact_limit(claim)
        relative_path = _output_relative_path(parsed.path)
        source_path = roots.assigned_output(relative_path)
        claim_payload = getattr(claim, "payload", None)
        if not isinstance(claim_payload, Mapping):
            raise ArtifactValidationError("artifact_assignment_missing")
        assigned_keys = tuple(
            key
            for key in ("assigned_path", "artifact_path", "output_path")
            if key in claim_payload
        )
        if not assigned_keys:
            raise ArtifactValidationError("artifact_assignment_missing")
        assigned = claim_payload[assigned_keys[0]]
        if not isinstance(assigned, str):
            raise ArtifactValidationError("artifact_assignment_invalid")
        if relative_path != _output_relative_path(assigned):
            raise ArtifactValidationError("artifact_unassigned_output")
        declared_lineage = claim_payload.get("lineage")
        if (
            isinstance(declared_lineage, (str, bytes))
            or not isinstance(declared_lineage, (list, tuple))
        ):
            raise ArtifactValidationError("artifact_lineage_missing")
        declared_lineage_tuple = tuple(declared_lineage)
        if expected_lineage is not None:
            if tuple(expected_lineage) != declared_lineage_tuple:
                raise ArtifactValidationError("artifact_lineage_assignment_mismatch")
        else:
            expected_lineage = declared_lineage_tuple
        if expected_path is not None:
            expected_relative = _output_relative_path(str(expected_path))
            if relative_path != expected_relative:
                raise ArtifactValidationError("artifact_unassigned_output")
        if source_path.is_symlink() or not source_path.is_file():
            raise ArtifactValidationError("artifact_file_missing")
        digest, size = digest_file(source_path)
        if digest != parsed.content_digest:
            raise ArtifactValidationError("artifact_digest_mismatch")
        if size != parsed.size_bytes:
            raise ArtifactValidationError("artifact_size_mismatch")
        if expected_lineage is not None and tuple(expected_lineage) != parsed.lineage:
            raise ArtifactValidationError("artifact_lineage_mismatch")
        try:
            self._validate_lineage(claim, parsed.lineage)
        except ArtifactError as exc:
            raise ArtifactValidationError(
                "artifact_lineage_parent_integrity"
            ) from exc
        self._validate_pinned_digests(parsed, claim.run_id)
        admitted_path = self._admitted_path(digest)
        artifact_id = _artifact_id(parsed)
        return PreparedArtifact(parsed, source_path, admitted_path, artifact_id)

    def admit(
        self,
        claim: Any,
        envelope: ArtifactEnvelope | Mapping[str, Any],
        *,
        expected_lineage: tuple[str, ...] | list[str] | None = None,
        expected_path: str | Path | None = None,
    ) -> ArtifactRecord:
        """Admit one artifact record without settling its work item."""

        prepared = self.prepare(
            claim,
            envelope,
            expected_lineage=expected_lineage,
            expected_path=expected_path,
        )
        try:
            with self.store._transaction() as connection:
                self._assert_current(connection, claim)
                self.materialize(prepared)
                self._assert_current(connection, claim)
                return self.admit_prepared(connection, claim.run_id, prepared)
        except ArtifactError:
            self.discard_unreferenced(prepared)
            raise

    admit_artifact = admit

    def materialize(self, prepared: PreparedArtifact) -> Path:
        """Copy verified attempt output into the content-addressed store."""

        self.content_dir.mkdir(parents=True, exist_ok=True)
        if self.content_dir.is_symlink():
            raise ArtifactPathError("artifact_store_symlink")
        expected_path = self._admitted_path(prepared.envelope.content_digest)
        if prepared.admitted_path != expected_path:
            raise ArtifactPathError("artifact_store_path_invalid")
        admitted_path = prepared.admitted_path
        if admitted_path.exists() and admitted_path.is_symlink():
            raise ArtifactPathError("artifact_store_symlink")
        if admitted_path.exists():
            stored_digest, stored_size = digest_file(admitted_path)
            if (
                stored_digest != prepared.envelope.content_digest
                or stored_size != prepared.envelope.size_bytes
            ):
                raise ArtifactError("artifact_store_digest_mismatch")
            return admitted_path
        temporary = self.content_dir / (
            f".{prepared.envelope.content_digest.removeprefix(_DIGEST_PREFIX)}.{uuid.uuid4().hex}"
        )
        try:
            with prepared.source_path.open("rb") as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target, length=64 * 1024)
                target.flush()
                os.fsync(target.fileno())
            copied_digest, copied_size = digest_file(temporary)
            if (
                copied_digest != prepared.envelope.content_digest
                or copied_size != prepared.envelope.size_bytes
            ):
                raise ArtifactError("artifact_copy_digest_mismatch")
            os.replace(temporary, admitted_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return admitted_path

    copy_to_content_store = materialize

    def discard_unreferenced(self, prepared: PreparedArtifact) -> None:
        path = prepared.admitted_path
        with self.store._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM executor_artifacts
                WHERE state = 'admitted' AND admitted_path = ?
                """,
                (str(path),),
            ).fetchone()
        if row is not None and int(row["total"]) == 0:
            if path.is_file() and not path.is_symlink():
                path.unlink()

    def admit_prepared(
        self,
        connection: Any,
        run_id: str,
        prepared: PreparedArtifact,
        *,
        state: str = "admitted",
        error_code: str | None = None,
        created_at: float | None = None,
    ) -> ArtifactRecord:
        if state not in {"admitted", "rejected", "stale"}:
            raise ArtifactValidationError("artifact_state_invalid")
        envelope = prepared.envelope
        envelope_json = canonical_json(envelope.to_dict())
        lineage_json = canonical_json(list(envelope.lineage))
        pinned_json = canonical_json(dict(envelope.pinned_digests))
        now = (
            float(created_at)
            if created_at is not None
            else float(self._clock() if self._clock is not None else time.time())
        )
        existing = connection.execute(
            """
            SELECT * FROM executor_artifacts
            WHERE run_id = ? AND artifact_id = ?
            """,
            (run_id, prepared.artifact_id),
        ).fetchone()
        if existing is not None:
            existing_record = self._record(existing)
            if (
                existing_record.envelope != envelope.to_dict()
                or existing_record.state != state
                or existing_record.error_code != error_code
            ):
                raise ArtifactError("artifact_record_conflict")
            return existing_record
        connection.execute(
            """
            INSERT INTO executor_artifacts(
                artifact_id, run_id, work_id, attempt_id, fencing_token,
                artifact_type, path, content_digest, size_bytes, lineage_json,
                pinned_digests_json, harness, worker, agent, pane, state,
                error_code, envelope_json, admitted_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prepared.artifact_id,
                run_id,
                envelope.work_id,
                envelope.attempt_id,
                envelope.fencing_token,
                envelope.artifact_type,
                envelope.path,
                envelope.content_digest,
                envelope.size_bytes,
                lineage_json,
                pinned_json,
                envelope.harness,
                envelope.worker,
                envelope.agent,
                envelope.pane,
                state,
                error_code,
                envelope_json,
                str(prepared.admitted_path) if state == "admitted" else None,
                now,
            ),
        )
        row = connection.execute(
            """
            SELECT * FROM executor_artifacts
            WHERE run_id = ? AND artifact_id = ?
            """,
            (run_id, prepared.artifact_id),
        ).fetchone()
        if row is None:
            raise ArtifactError("artifact_record_insert_failed")
        return self._record(row)

    def reject(
        self,
        claim: Any,
        *,
        error_code: str,
        envelope: ArtifactEnvelope | Mapping[str, Any] | None = None,
        stale: bool = False,
    ) -> ArtifactRecord:
        """Record a failed submission without changing work progress."""

        self.initialize()
        if not isinstance(error_code, str) or not error_code:
            raise ArtifactValidationError("artifact_error_code_invalid")
        parsed: ArtifactEnvelope | None
        try:
            parsed = (
                envelope
                if isinstance(envelope, ArtifactEnvelope)
                else ArtifactEnvelope.from_mapping(envelope)
                if envelope is not None
                else None
            )
        except ArtifactError:
            parsed = None
        state = "stale" if stale else "rejected"
        artifact_id = (
            _artifact_id(parsed)
            if parsed is not None
            else f"{state}_{getattr(claim, 'attempt_id', 'unknown')}_{error_code}"
        )
        if parsed is None:
            parsed = _placeholder_envelope(claim)
        else:
            submitted = parsed.to_dict()
            parsed = replace(
                parsed,
                run_id=str(getattr(claim, "run_id")),
                work_id=str(getattr(claim, "work_id")),
                logical_id=str(getattr(claim, "work_id")),
                attempt_id=str(getattr(claim, "attempt_id")),
                fencing_token=str(getattr(claim, "fencing_token")),
                harness=str(getattr(claim, "harness")),
                worker=str(getattr(claim, "worker")),
                agent=str(getattr(claim, "agent_name")),
                payload={"submitted": submitted, "payload": parsed.payload},
            )
        prepared = PreparedArtifact(
            parsed,
            self.roots(claim).out,
            self.content_dir / "missing",
            artifact_id,
        )
        with self.store._transaction() as connection:
            return self.admit_prepared(
                connection,
                claim.run_id,
                prepared,
                state=state,
                error_code=error_code,
            )

    record_rejection = reject

    def list_artifacts(
        self,
        run_id: str,
        *,
        work_id: str | None = None,
    ) -> list[ArtifactRecord]:
        self.store.require_run(run_id)
        self.store.initialize()
        with self.store._connect() as connection:
            query = (
                "SELECT * FROM executor_artifacts WHERE run_id = "
                "? "
                + ("AND work_id = ? " if work_id is not None else "")
                + "ORDER BY created_at, artifact_id"
            )
            values: tuple[Any, ...] = (
                (run_id, work_id) if work_id is not None else (run_id,)
            )
            rows = connection.execute(query, values).fetchall()
        return [self._record(row) for row in rows]

    artifacts = list_artifacts

    def _record(self, row: Any) -> ArtifactRecord:
        record = ArtifactRecord(
            artifact_id=str(row["artifact_id"]),
            run_id=str(row["run_id"]),
            work_id=str(row["work_id"]),
            attempt_id=str(row["attempt_id"]),
            fencing_token=str(row["fencing_token"]),
            artifact_type=str(row["artifact_type"]),
            path=str(row["path"]),
            content_digest=str(row["content_digest"]),
            size_bytes=int(row["size_bytes"]),
            lineage=tuple(json.loads(str(row["lineage_json"]))),
            pinned_digests=dict(json.loads(str(row["pinned_digests_json"]))),
            harness=str(row["harness"]),
            worker=str(row["worker"]),
            agent=str(row["agent"]),
            pane=None if row["pane"] is None else str(row["pane"]),
            state=str(row["state"]),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            envelope=dict(json.loads(str(row["envelope_json"]))),
            admitted_path=(
                None if row["admitted_path"] is None else str(row["admitted_path"])
            ),
            created_at=float(row["created_at"]),
        )
        if record.state == "admitted":
            self._verify_admitted_content(record)
        return record

    def _verify_admitted_content(self, record: ArtifactRecord) -> None:
        if record.admitted_path is None:
            raise ArtifactError("artifact_content_integrity_failed: path_missing")
        path = Path(record.admitted_path)
        content_root = self.content_dir.resolve(strict=False)
        if path.is_symlink() or not path.resolve(strict=False).is_relative_to(
            content_root
        ):
            raise ArtifactError("artifact_content_integrity_failed: path_escape")
        if not path.is_file():
            raise ArtifactError("artifact_content_integrity_failed: file_missing")
        try:
            digest, size = digest_file(path)
        except ArtifactError as exc:
            raise ArtifactError("artifact_content_integrity_failed: unreadable") from exc
        if digest != record.content_digest or size != record.size_bytes:
            raise ArtifactError("artifact_content_integrity_failed: digest_mismatch")

    def _validate_pinned_digests(self, envelope: ArtifactEnvelope, run_id: str) -> None:
        unknown = set(envelope.pinned_digests) - set(MANIFEST_DIGEST_FIELDS)
        if unknown:
            raise ArtifactValidationError(
                f"artifact_pinned_digest_unknown:{sorted(unknown, key=str)[0]}"
            )
        missing = set(MANIFEST_DIGEST_FIELDS) - set(envelope.pinned_digests)
        if missing:
            raise ArtifactValidationError(
                f"artifact_pinned_digest_missing:{sorted(missing)[0]}"
            )
        try:
            manifest = self.store.require_run(run_id).manifest
        except Exception as exc:
            raise ArtifactValidationError("artifact_run_not_found") from exc
        for field, expected in (
            (field, getattr(manifest, field)) for field in envelope.pinned_digests
        ):
            if envelope.pinned_digests[field] != expected:
                raise ArtifactValidationError(f"artifact_{field}_mismatch")

    def _enforce_attempt_artifact_limit(self, claim: Any) -> None:
        with self.store._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM executor_artifacts
                WHERE run_id = ? AND attempt_id = ? AND fencing_token = ?
                """,
                (claim.run_id, claim.attempt_id, claim.fencing_token),
            ).fetchone()
        if row is not None and int(row["total"]) >= MAX_ARTIFACTS_PER_ATTEMPT:
            raise ArtifactValidationError("artifact_count_limit")

    def _validate_lineage(self, claim: Any, lineage: tuple[str, ...]) -> None:
        parents = set(lineage) - {claim.work_id}
        if not parents:
            return
        with self.store._connect() as connection:
            artifact_rows = connection.execute(
                """
                SELECT *
                FROM executor_artifacts
                WHERE run_id = ? AND state = 'admitted'
                """,
                (claim.run_id,),
            ).fetchall()
        known_artifacts: set[str] = set()
        for row in artifact_rows:
            record = self._record(row)
            known_artifacts.add(record.artifact_id)
            known_artifacts.add(record.content_digest)
            known_artifacts.add(record.envelope["logical_id"])
        missing = parents - known_artifacts
        if missing:
            raise ArtifactValidationError(
                f"artifact_lineage_parent_missing:{sorted(missing)[0]}"
            )

    def _assert_current(self, connection: Any, claim: Any) -> None:
        row = connection.execute(
            """
            SELECT a.lease_until, a.state, w.state AS work_state,
                   w.attempt_id, w.fencing_token, w.claim_id
            FROM executor_attempts a
            JOIN executor_work_items w
              ON w.run_id = a.run_id AND w.work_id = a.work_id
            WHERE a.run_id = ? AND a.work_id = ? AND a.attempt_id = ?
              AND a.fencing_token = ? AND a.claim_id = ?
            """,
            (
                claim.run_id,
                claim.work_id,
                claim.attempt_id,
                claim.fencing_token,
                claim.claim_id,
            ),
        ).fetchone()
        now = float(self._clock() if self._clock is not None else time.time())
        if (
            row is None
            or str(row["state"]) != "running"
            or str(row["work_state"]) != "running"
            or float(row["lease_until"]) <= now
            or str(row["attempt_id"]) != claim.attempt_id
            or str(row["fencing_token"]) != claim.fencing_token
            or str(row["claim_id"]) != claim.claim_id
        ):
            raise ArtifactValidationError("stale_attempt")

    def _admitted_path(self, digest: str) -> Path:
        self.content_dir.mkdir(parents=True, exist_ok=True)
        if self.content_dir.is_symlink():
            raise ArtifactPathError("artifact_store_symlink")
        path = self.content_dir / digest.removeprefix(_DIGEST_PREFIX)
        if not path.resolve(strict=False).is_relative_to(self.content_dir.resolve(strict=False)):
            raise ArtifactPathError("artifact_store_escape")
        return path


def digest_bytes(content: bytes) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(content).hexdigest()


def digest_file(path: Path | str, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> tuple[str, int]:
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise ArtifactValidationError("artifact_file_missing") from exc
    if size > max_bytes:
        raise ArtifactValidationError("artifact_oversize")
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return _DIGEST_PREFIX + digest.hexdigest(), size


def atomic_write_bytes(path: Path | str, content: bytes) -> tuple[str, int]:
    """Write a bounded artifact atomically and return its verified digest."""

    destination = Path(path)
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ArtifactValidationError("artifact_oversize")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest_bytes(content), len(content)


def _identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or value.strip() != value
    ):
        raise ArtifactValidationError(f"{field}_invalid")
    if len(value) > 128:
        raise ArtifactValidationError(f"{field}_too_long")
    return value


def _relative_path(value: str | Path, *, field: str) -> Path:
    if isinstance(value, Path):
        candidate = value
    elif isinstance(value, str):
        candidate = Path(value)
    else:
        raise ArtifactPathError(f"{field}_invalid")
    if not str(candidate) or "\x00" in str(candidate):
        raise ArtifactPathError(f"{field}_invalid")
    if candidate.is_absolute():
        raise ArtifactPathError(f"{field}_absolute")
    if len(str(candidate)) > MAX_ARTIFACT_PATH_LENGTH:
        raise ArtifactPathError(f"{field}_too_long")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ArtifactPathError(f"{field}_escape")
    return candidate


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field}_invalid")
    digest = value.removeprefix(_DIGEST_PREFIX)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ArtifactValidationError(f"{field}_invalid")
    return _DIGEST_PREFIX + digest


def _output_relative_path(value: str) -> Path:
    candidate = _relative_path(value, field="artifact_path")
    if candidate.parts and candidate.parts[0] == "out":
        if len(candidate.parts) == 1:
            raise ArtifactPathError("artifact_path_invalid")
        candidate = Path(*candidate.parts[1:])
    return candidate


def _validate_envelope_against_claim(
    envelope: ArtifactEnvelope,
    claim: Any,
) -> None:
    for field in ("run_id", "work_id", "attempt_id", "fencing_token"):
        expected = getattr(claim, field, None)
        if getattr(envelope, field) != expected:
            raise ArtifactValidationError(f"artifact_{field}_mismatch")
    if envelope.logical_id != envelope.work_id:
        raise ArtifactValidationError("artifact_logical_id_mismatch")
    if envelope.harness != getattr(claim, "harness", None):
        raise ArtifactValidationError("artifact_harness_mismatch")
    if envelope.worker != getattr(claim, "worker", None):
        raise ArtifactValidationError("artifact_worker_mismatch")
    if envelope.agent != getattr(claim, "agent_name", None):
        raise ArtifactValidationError("artifact_agent_mismatch")
    expected_pane = f"pane:{getattr(claim, 'agent_name', '')}"
    if envelope.pane != expected_pane:
        raise ArtifactValidationError("artifact_pane_mismatch")


def _artifact_id(envelope: ArtifactEnvelope) -> str:
    canonical = canonical_json(
        {
            "run_id": envelope.run_id,
            "work_id": envelope.work_id,
            "attempt_id": envelope.attempt_id,
            "fencing_token": envelope.fencing_token,
            "path": envelope.path,
            "content_digest": envelope.content_digest,
            "artifact_type": envelope.artifact_type,
        }
    )
    return "artifact_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _placeholder_envelope(claim: Any) -> ArtifactEnvelope:
    from herdr_orchestrator.executor_protocol import EMPTY_DIGEST

    return ArtifactEnvelope(
        contract_version=ARTIFACT_CONTRACT_VERSION,
        artifact_type="rejected",
        run_id=str(getattr(claim, "run_id", "unknown")),
        work_id=str(getattr(claim, "work_id", "unknown")),
        logical_id=str(getattr(claim, "work_id", "unknown")),
        attempt_id=str(getattr(claim, "attempt_id", "unknown")),
        fencing_token=str(getattr(claim, "fencing_token", "unknown")),
        path="rejected",
        content_digest=digest_bytes(b""),
        size_bytes=0,
        lineage=(),
        pinned_digests={field: EMPTY_DIGEST for field in MANIFEST_DIGEST_FIELDS},
        harness=str(getattr(claim, "harness", "unknown")),
        worker=str(getattr(claim, "worker", "unknown")),
        agent=str(getattr(claim, "agent_name", "unknown")),
        pane=None,
        payload=None,
    )


def _remove_owned_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if not path.is_dir():
        return
    for child in path.iterdir():
        _remove_owned_tree(child)
    path.rmdir()
