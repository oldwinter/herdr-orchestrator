"""Atomic publication helpers for quality command result files."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from quality_validation import QualityBundleError

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


@contextmanager
def run_lock(root: Path, run_id: str) -> Iterator[None]:
    locks = root / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        locks / run_id, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise QualityBundleError("quality_run_reused")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise QualityBundleError("quality_run_reused") from error
        yield
    finally:
        os.close(descriptor)


def validate_owner(directory: Path) -> int:
    owner = directory / "owner.json"
    if directory.is_symlink() or not directory.is_dir() or owner.is_symlink():
        raise QualityBundleError("quality_run_reused")
    try:
        payload = json.loads(
            artifact_snapshot(owner).data, object_pairs_hook=object_without_duplicates
        )
        owner_pid = payload["pid"]
        if (
            type(owner_pid) is not int
            or not 0 < owner_pid <= 2**31 - 1
            or payload.get("run_id", directory.name) != directory.name
        ):
            raise ValueError("invalid owner")
    except (OSError, TypeError, ValueError, KeyError) as error:
        raise QualityBundleError("quality_run_reused") from error
    return owner_pid


def require_dead_owner(pending: Path) -> None:
    owner_pid = validate_owner(pending)
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise QualityBundleError("quality_run_reused") from error
    raise QualityBundleError("quality_run_reused")


def claim_pending(pending: Path) -> None:
    claims = pending.parent.parent / ".claims"
    claims.mkdir(parents=True, exist_ok=True)
    prepared = Path(tempfile.mkdtemp(prefix=f"{pending.name}.", dir=claims))
    atomic_write_json(prepared / "owner.json", {"pid": os.getpid(), "run_id": pending.name})
    os.replace(prepared, pending)


def discard_pending(pending: Path) -> None:
    claims = pending.parent.parent / ".claims"
    claims.mkdir(parents=True, exist_ok=True)
    retired = Path(tempfile.mkdtemp(prefix=f"{pending.name}.", suffix=".retired", dir=claims))
    os.replace(pending, retired)
    shutil.rmtree(retired)


def claim_run_directory(pending: Path, final: Path, *, reuse_completed: bool) -> None:
    if final.exists() or final.is_symlink():
        raise QualityBundleError("quality_run_reused")
    if pending.exists() or pending.is_symlink():
        require_dead_owner(pending)
        if (pending / "manifest.json").exists():
            if not reuse_completed:
                raise QualityBundleError("quality_run_reused")
            os.replace(pending, final)
            return
        discard_pending(pending)
    claim_pending(pending)


@dataclass(frozen=True)
class ArtifactSnapshot:
    data: bytes
    device: int
    inode: int
    size: int
    modified_ns: int


def object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def json_object(path: Path, error_code: str) -> dict[str, object]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=object_without_duplicates,
            parse_constant=reject_constant,
        )
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise QualityBundleError(error_code) from error
    if not isinstance(payload, dict):
        raise QualityBundleError(error_code)
    return payload


def _signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def artifact_snapshot(path: Path) -> ArtifactSnapshot:
    if path.is_symlink():
        raise QualityBundleError("quality_artifact_path_invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise QualityBundleError("quality_artifact_missing") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ARTIFACT_BYTES:
            raise QualityBundleError("quality_artifact_invalid")
        chunks: list[bytes] = []
        remaining = MAX_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError as error:
        raise QualityBundleError("quality_artifact_changed") from error
    signature = _signature(before)
    if len(data) > MAX_ARTIFACT_BYTES or signature != _signature(after):
        raise QualityBundleError("quality_artifact_changed")
    if signature != _signature(current):
        raise QualityBundleError("quality_artifact_changed")
    return ArtifactSnapshot(data, *signature)


def assert_artifact_unchanged(path: Path, snapshot: ArtifactSnapshot) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise QualityBundleError("quality_artifact_changed") from error
    expected = snapshot.device, snapshot.inode, snapshot.size, snapshot.modified_ns
    if _signature(current) != expected:
        raise QualityBundleError("quality_artifact_changed")


def parse_artifact_bytes(data: bytes, parser: str) -> dict[str, object] | None:
    if parser == "json":
        try:
            payload = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=object_without_duplicates,
                parse_constant=reject_constant,
            )
        except (UnicodeError, ValueError, RecursionError) as error:
            raise QualityBundleError("quality_artifact_invalid") from error
        if not isinstance(payload, dict):
            raise QualityBundleError("quality_artifact_invalid")
        return payload
    if parser == "nonempty":
        if not data:
            raise QualityBundleError("quality_artifact_invalid")
        return None
    if parser == "pstats":
        import pstats

        descriptor, temporary = tempfile.mkstemp(prefix="quality-profile-", suffix=".pstats")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            pstats.Stats(temporary)
        except Exception as error:  # pstats exposes several parser-specific exception types.
            raise QualityBundleError("quality_artifact_invalid") from error
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
        return None
    raise QualityBundleError("quality_artifact_parser_invalid")


def inspect_artifact(path: Path, parser: str) -> tuple[str, dict[str, object] | None, bytes]:
    snapshot = artifact_snapshot(path)
    digest = hashlib.sha256(snapshot.data).hexdigest()
    payload = parse_artifact_bytes(snapshot.data, parser)
    assert_artifact_unchanged(path, snapshot)
    return digest, payload, snapshot.data


def publish_results(
    *,
    default_path: Path,
    requested_path: Path | None,
    payload: object,
    write_json: Callable[[Path, object], None],
) -> None:
    paths = [default_path]
    if requested_path is not None and requested_path != default_path:
        paths.append(requested_path)
    locks: list[tuple[Path, int]] = []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
        for path in paths:
            lock = path.with_name(f".{path.name}.quality-lock")
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                os.close(descriptor)
                raise OSError("quality_result_busy") from error
            locks.append((lock, descriptor))
    except OSError:
        for _, descriptor in locks:
            os.close(descriptor)
        raise
    new_paths = [path for path in paths if not path.exists()]
    try:
        for path in paths:
            write_json(path, payload)
    except (OSError, RuntimeError, shutil.Error):
        for path in new_paths:
            with suppress(OSError):
                path.unlink()
        for path in paths:
            try:
                if path.is_file() and path.stat().st_size == 0:
                    path.unlink()
            except OSError:
                pass
        raise
    finally:
        for _, descriptor in locks:
            os.close(descriptor)
