#!/usr/bin/env python3
"""Create run-scoped quality evidence bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

try:
    import quality_storage as _quality_storage
    import quality_validation as _quality_validation
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import quality_storage as _quality_storage
    import quality_validation as _quality_validation

QualityBundleError = _quality_validation.QualityBundleError
_inventory_digest = _quality_validation.inventory_digest
_validate_artifact_payload = _quality_validation.validate_artifact_payload

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
COMMIT = re.compile(r"[0-9a-fA-F]{40}")
NAME = re.compile(r"[a-z][a-z0-9-]*")
SHA256 = re.compile(r"[0-9a-f]{64}")
OUTPUT_DIRECTORY = "${QUALITY_OUTPUT_DIR}"
COMMAND_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class ArtifactSpec:
    key: str
    relative_path: str
    parser: Literal["json", "nonempty", "pstats"]


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    tool: str
    version_argv: tuple[str, ...]
    stdout_artifact: str | None = None
    include_tracked_files: bool = False


@dataclass(frozen=True)
class ProducerSpec:
    name: str
    commands: tuple[CommandSpec, ...]
    artifacts: tuple[ArtifactSpec, ...]


@dataclass(frozen=True)
class CompletedBundle:
    path: Path
    manifest_path: Path
    passed: bool
    exit_code: int


@dataclass(frozen=True)
class SourceIdentity:
    commit: str
    digest: str
    clean: bool


@dataclass(frozen=True)
class RunResult:
    path: Path
    bundle_path: Path
    manifest_path: Path
    commit: str
    invocation_id: str
    run_id: str
    source_digest: str
    source_clean: bool
    passed: bool
    exit_code: int


@dataclass(frozen=True)
class ArtifactEvidence:
    key: str
    path: Path
    sha256: str
    verified: bool
    payload: dict[str, object] | None


@dataclass(frozen=True)
class ProducerEvidence:
    name: str
    outcome: Literal["passed", "failed"]
    verification: Literal["verified", "not_verified"]
    artifacts: tuple[ArtifactEvidence, ...]


@dataclass(frozen=True)
class QualityManifest:
    path: Path
    commit: str
    invocation_id: str
    run_id: str
    started_at: str
    completed_at: str
    source_digest: str
    ending_source_digest: str
    source_clean: bool
    producers: tuple[ProducerEvidence, ...]


@dataclass(frozen=True)
class CommandInvocation:
    argv: tuple[str, ...]
    input_count: int
    input_sha256: str


@dataclass(frozen=True)
class ValidatedCommand:
    tool: str
    tool_version: str
    outcome: Literal["passed", "failed"]
    exit_code: int
    started_at: datetime
    ended_at: datetime


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _run_id(commit: str, invocation_id: str, source_digest: str) -> str:
    if COMMIT.fullmatch(commit) is None:
        raise QualityBundleError("quality_commit_invalid")
    if not invocation_id or len(invocation_id) > 200:
        raise QualityBundleError("quality_invocation_invalid")
    if SHA256.fullmatch(source_digest) is None:
        raise QualityBundleError("quality_source_digest_invalid")
    identity = f"{commit.lower()}\0{invocation_id}\0{source_digest}".encode()
    return f"{commit[:12].lower()}-{hashlib.sha256(identity).hexdigest()[:20]}"


_atomic_write_json = _quality_storage.atomic_write_json
_atomic_write_bytes = _quality_storage.atomic_write_bytes
_json_bytes = _quality_storage.json_bytes


def _artifact_path(output_dir: Path, spec: ArtifactSpec) -> Path:
    relative = Path(spec.relative_path)
    if relative.is_absolute() or ".." in relative.parts or not spec.key:
        raise QualityBundleError("quality_artifact_path_invalid")
    path = output_dir / relative
    if path.is_symlink() or not path.is_file():
        raise QualityBundleError("quality_artifact_missing")
    return path


_object_without_duplicates = _quality_storage.object_without_duplicates
_artifact_snapshot = _quality_storage.artifact_snapshot
_assert_artifact_unchanged = _quality_storage.assert_artifact_unchanged
_parse_artifact_bytes = _quality_storage.parse_artifact_bytes
_inspect_artifact = _quality_storage.inspect_artifact


def _tool_version(command: CommandSpec) -> tuple[str, bool]:
    try:
        result = subprocess.run(
            command.version_argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable", False
    text = (result.stdout or result.stderr).strip().splitlines()
    version = text[0][:256] if text else "unavailable"
    return version, result.returncode == 0 and version != "unavailable"


def _tracked_files() -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QualityBundleError("quality_inventory_unavailable") from error
    if result.returncode != 0:
        raise QualityBundleError("quality_inventory_unavailable")
    try:
        names = result.stdout.decode().split("\0")
    except UnicodeError as error:
        raise QualityBundleError("quality_inventory_invalid") from error
    return tuple(name for name in names if name)


def _expanded_argv(command: CommandSpec, output_dir: Path) -> CommandInvocation:
    argv = tuple(argument.replace(OUTPUT_DIRECTORY, str(output_dir)) for argument in command.argv)
    inputs = _tracked_files() if command.include_tracked_files else ()
    return CommandInvocation((*argv, *inputs), len(inputs), _inventory_digest(inputs))


def _claim_run_directory(pending: Path, final: Path) -> None:
    if final.exists():
        raise QualityBundleError("quality_run_reused")
    try:
        pending.mkdir()
    except FileExistsError:
        _quality_storage.reclaim_pending(pending)
        try:
            pending.mkdir()
        except FileExistsError as error:
            raise QualityBundleError("quality_run_reused") from error
    _atomic_write_json(pending / "owner.json", {"pid": os.getpid()})
    if final.exists():
        raise QualityBundleError("quality_run_reused")


def run_quality(
    *,
    root: Path,
    commit: str,
    invocation_id: str,
    specs: Sequence[ProducerSpec],
    source: SourceIdentity | None = None,
    source_probe: Callable[[], SourceIdentity] | None = None,
    reuse_completed: bool = False,
) -> CompletedBundle:
    """Run producer specs and atomically publish one completed bundle."""
    if source is None:
        source = SourceIdentity(
            commit.lower(),
            hashlib.sha256(f"fixture\0{commit.lower()}".encode()).hexdigest(),
            True,
        )
    if source.commit != commit.lower():
        raise QualityBundleError("quality_commit_mismatch")
    run_id = _run_id(commit, invocation_id, source.digest)
    pending_root = root / ".pending"
    runs_root = root / "runs"
    pending_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)
    pending = pending_root / run_id
    final = runs_root / run_id
    if reuse_completed and final.is_dir():
        manifest = load_completed_manifest(
            final / "manifest.json",
            expected_commit=commit,
            expected_invocation_id=invocation_id,
            expected_run_id=run_id,
            expected_source_digest=source.digest,
            expected_specs=specs,
            source_probe=source_probe,
        )
        passed = (
            enforce_manifest(
                manifest, required_producers=tuple(producer.name for producer in manifest.producers)
            )
            == 0
        )
        return CompletedBundle(final, final / "manifest.json", passed, 0 if passed else 1)
    _claim_run_directory(pending, final)
    (pending / "producers").mkdir()
    _atomic_write_json(pending / "owner.json", {"pid": os.getpid()})
    started_at = _utc_now()
    producer_payloads: list[dict[str, object]] = []
    seen: set[str] = set()
    all_passed = True
    bundle_exit_code = 0
    for producer in specs:
        if NAME.fullmatch(producer.name) is None or producer.name in seen:
            raise QualityBundleError("quality_producer_invalid")
        seen.add(producer.name)
        work_dir = pending / f".{producer.name}-{uuid.uuid4().hex}.work"
        publish_dir = pending / f".{producer.name}-{uuid.uuid4().hex}.publish"
        work_dir.mkdir()
        publish_dir.mkdir()
        environment = os.environ.copy()
        environment["QUALITY_OUTPUT_DIR"] = str(work_dir)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(ROOT / "src"), environment.get("PYTHONPATH")))
        )
        producer_started = _utc_now()
        commands: list[dict[str, object]] = []
        producer_passed = True
        tool_versions: dict[str, str] = {}
        for command in producer.commands:
            version, version_verified = _tool_version(command)
            tool_versions[command.tool] = version
            command_started = _utc_now()
            error_code: str | None = None
            try:
                invocation = _expanded_argv(command, work_dir)
            except QualityBundleError as error:
                invocation = CommandInvocation(command.argv, 0, hashlib.sha256().hexdigest())
                error_code = str(error)
                exit_code = 1
            else:
                stdout_path = None
                stdout = None
                if command.stdout_artifact is not None:
                    stdout_path = work_dir / command.stdout_artifact
                    stdout_path.parent.mkdir(parents=True, exist_ok=True)
                    stdout = stdout_path.open("wb")
                try:
                    try:
                        result = subprocess.run(
                            invocation.argv,
                            cwd=ROOT,
                            env=environment,
                            stdout=stdout,
                            check=False,
                            timeout=COMMAND_TIMEOUT_SECONDS,
                        )
                        exit_code = result.returncode
                    except subprocess.TimeoutExpired:
                        exit_code = 124
                        error_code = "quality_command_timeout"
                    except OSError:
                        exit_code = 127
                        error_code = "quality_command_unavailable"
                finally:
                    if stdout is not None:
                        stdout.flush()
                        os.fsync(stdout.fileno())
                        stdout.close()
            command_ended = _utc_now()
            command_passed = exit_code == 0 and version_verified and error_code is None
            producer_passed = producer_passed and command_passed
            if bundle_exit_code == 0 and not command_passed:
                bundle_exit_code = exit_code or 1
            command_payload: dict[str, object] = {
                "argv": list(command.argv),
                "argv_count": len(invocation.argv),
                "ended_at": command_ended,
                "exit_code": exit_code,
                "executed_argv": [
                    argument.replace(str(work_dir), OUTPUT_DIRECTORY)
                    for argument in invocation.argv
                ],
                "input_count": invocation.input_count,
                "input_sha256": invocation.input_sha256,
                "outcome": "passed" if command_passed else "failed",
                "started_at": command_started,
                "tool": command.tool,
                "tool_version": version,
            }
            if error_code is not None:
                command_payload["error_code"] = error_code
            commands.append(command_payload)
        parsed_artifacts: list[tuple[ArtifactSpec, bytes, str]] = []
        expected_exit_code = next(
            (
                command["exit_code"]
                for command in commands
                if isinstance(command.get("exit_code"), int)
            ),
            None,
        )
        expected_branch = _quality_validation.expected_branch_coverage(producer.commands)
        coverage_threshold = _quality_validation.expected_coverage_threshold(producer.commands)
        build_command = _quality_validation.expected_build_command(producer.commands)
        for artifact in producer.artifacts:
            try:
                path = _artifact_path(work_dir, artifact)
                digest, payload, data = _inspect_artifact(path, artifact.parser)
                if artifact.parser == "json":
                    if payload is None:
                        raise QualityBundleError("quality_artifact_invalid")
                    _validate_artifact_payload(
                        producer.name,
                        artifact.key,
                        payload,
                        expected_runs=_quality_validation.expected_runs(producer.commands),
                        expected_exit_code=(
                            expected_exit_code
                            if artifact.key in {"tests", "stability", "build"}
                            else None
                        ),
                        expected_branch=expected_branch,
                        coverage_threshold=coverage_threshold,
                        build_command=build_command,
                    )
                    if _quality_validation.finding_count(artifact.key, payload):
                        producer_passed = False
                        bundle_exit_code = bundle_exit_code or 1
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                producer_passed = False
                if bundle_exit_code == 0:
                    bundle_exit_code = 1
                continue
            parsed_artifacts.append((artifact, data, digest))
        producer_ended = _utc_now()
        result_exit_code = next(
            (
                command["exit_code"]
                for command in commands
                if isinstance(command["exit_code"], int) and command["exit_code"] != 0
            ),
            0 if producer_passed else 1,
        )
        result_payload = {
            "ended_at": producer_ended,
            "exit_code": result_exit_code,
            "outcome": "passed" if producer_passed else "failed",
            "producer": producer.name,
            "started_at": producer_started,
            "tool_versions": tool_versions,
            "verification": "verified" if producer_passed else "not_verified",
        }
        result_data = _json_bytes(result_payload)
        parsed_artifacts.append(
            (
                ArtifactSpec("result", "result.json", "json"),
                result_data,
                hashlib.sha256(result_data).hexdigest(),
            )
        )
        artifact_payloads = [
            {
                "format": artifact.parser,
                "key": artifact.key,
                "path": f"producers/{producer.name}/{artifact.relative_path}",
                "sha256": digest,
                "verified": producer_passed,
            }
            for artifact, _, digest in parsed_artifacts
        ]
        producer_payload: dict[str, object] = {
            "artifacts": artifact_payloads,
            "commands": commands,
            "ended_at": producer_ended,
            "name": producer.name,
            "outcome": "passed" if producer_passed else "failed",
            "started_at": producer_started,
            "tool_versions": tool_versions,
            "verification": "verified" if producer_passed else "not_verified",
        }
        for artifact, data, _ in parsed_artifacts:
            _atomic_write_bytes(publish_dir / artifact.relative_path, data)
        _atomic_write_json(publish_dir / "producer.json", producer_payload)
        os.replace(publish_dir, pending / "producers" / producer.name)
        shutil.rmtree(work_dir)
        producer_payloads.append(producer_payload)
        all_passed = all_passed and producer_passed
    source_consistent = True
    ending_source_digest = source.digest
    if source_probe is not None:
        try:
            ending_source = source_probe()
        except (QualityBundleError, OSError, RuntimeError):
            source_consistent = False
            ending_source_digest = _quality_validation.SOURCE_PROBE_FAILURE_SHA256
        else:
            ending_source_digest = ending_source.digest
            source_consistent = ending_source == source
    if not source_consistent:
        all_passed = False
        if bundle_exit_code == 0:
            bundle_exit_code = 1
    with suppress(OSError):
        (pending / "owner.json").unlink()
    manifest = {
        "commit": commit.lower(),
        "completed_at": _utc_now(),
        "invocation_id": invocation_id,
        "producers": producer_payloads,
        "required_producers": [producer.name for producer in specs],
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        "source_clean": source.clean,
        "source_consistent": source_consistent,
        "source_digest": source.digest,
        "ending_source_digest": ending_source_digest,
        "started_at": started_at,
        "status": "completed",
    }
    _atomic_write_json(pending / "manifest.json", manifest)
    os.replace(pending, final)
    return CompletedBundle(final, final / "manifest.json", all_passed, bundle_exit_code)


def _json_object(path: Path, error_code: str) -> dict[str, object]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_quality_storage.reject_constant,
        )
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise QualityBundleError(error_code) from error
    if not isinstance(payload, dict):
        raise QualityBundleError(error_code)
    return payload


def _string(payload: dict[str, object], key: str, error_code: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise QualityBundleError(error_code)
    return value


def _timestamp(payload: dict[str, object], key: str, error_code: str) -> datetime:
    value = _string(payload, key, error_code)
    if not value.endswith("Z"):
        raise QualityBundleError(error_code)
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise QualityBundleError(error_code) from error
    if parsed.tzinfo != UTC:
        raise QualityBundleError(error_code)
    return parsed


def _validate_command(payload: object) -> ValidatedCommand:
    if not isinstance(payload, dict):
        raise QualityBundleError("quality_producer_invalid")
    argv = payload.get("argv")
    executed_argv = payload.get("executed_argv")
    exit_code = payload.get("exit_code")
    outcome_value = payload.get("outcome")
    argv_count = payload.get("argv_count")
    input_count = payload.get("input_count")
    input_sha256 = payload.get("input_sha256")
    error_code = payload.get("error_code")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(argument, str) and argument for argument in argv)
        or not isinstance(executed_argv, list)
        or not executed_argv
        or not all(isinstance(argument, str) and argument for argument in executed_argv)
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not isinstance(outcome_value, str)
        or outcome_value not in {"passed", "failed"}
        or not isinstance(argv_count, int)
        or isinstance(argv_count, bool)
        or argv_count < len(argv)
        or not isinstance(input_count, int)
        or isinstance(input_count, bool)
        or input_count < 0
        or not isinstance(input_sha256, str)
        or SHA256.fullmatch(input_sha256) is None
        or (error_code is not None and (not isinstance(error_code, str) or not error_code))
    ):
        raise QualityBundleError("quality_producer_invalid")
    started_at = _timestamp(payload, "started_at", "quality_producer_invalid")
    ended_at = _timestamp(payload, "ended_at", "quality_producer_invalid")
    tool = _string(payload, "tool", "quality_producer_invalid")
    tool_version = _string(payload, "tool_version", "quality_producer_invalid")
    outcome = cast(Literal["passed", "failed"], outcome_value)
    command_passed = exit_code == 0 and error_code is None and tool_version != "unavailable"
    if ended_at < started_at or (outcome == "passed") != command_passed:
        raise QualityBundleError("quality_producer_invalid")
    return ValidatedCommand(tool, tool_version, outcome, exit_code, started_at, ended_at)


def _validate_command_contract(payload: object, expected: CommandSpec) -> ValidatedCommand:
    validated = _validate_command(payload)
    if not isinstance(payload, dict):
        raise QualityBundleError("quality_command_mismatch")
    input_count = payload.get("input_count")
    inputs: tuple[str, ...] = ()
    if expected.include_tracked_files:
        if "--" not in expected.argv:
            raise QualityBundleError("quality_command_mismatch")
        inputs = tuple(_tracked_files())
        expected_input_count = len(inputs)
        expected_input_sha256 = _inventory_digest(inputs)
    else:
        expected_input_count = 0
        expected_input_sha256 = _quality_validation.EMPTY_INPUT_SHA256
    if (
        tuple(payload.get("argv", ())) != expected.argv
        or payload.get("tool") != expected.tool
        or tuple(payload.get("executed_argv", ())) != (*expected.argv, *inputs)
        or payload.get("argv_count") != len(expected.argv) + expected_input_count
        or input_count != expected_input_count
        or payload.get("input_sha256") != expected_input_sha256
    ):
        raise QualityBundleError("quality_command_mismatch")
    return validated


def load_completed_manifest(
    path: Path,
    *,
    expected_commit: str,
    expected_invocation_id: str,
    expected_run_id: str,
    expected_source_digest: str,
    require_clean: bool = False,
    expected_specs: Sequence[ProducerSpec] | None = None,
    source_probe: Callable[[], SourceIdentity] | None = None,
    expected_root: Path | None = None,
) -> QualityManifest:
    """Parse and verify one atomically published manifest and its artifacts."""
    if path.is_symlink() or not path.is_file() or path.name != "manifest.json":
        raise QualityBundleError("quality_manifest_missing")
    payload = _json_object(path, "quality_manifest_invalid")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("status") != "completed":
        raise QualityBundleError("quality_manifest_incomplete")
    commit = _string(payload, "commit", "quality_manifest_invalid").lower()
    invocation_id = _string(payload, "invocation_id", "quality_manifest_invalid")
    run_id = _string(payload, "run_id", "quality_manifest_invalid")
    source_digest = _string(payload, "source_digest", "quality_manifest_invalid")
    ending_source_digest = _string(payload, "ending_source_digest", "quality_manifest_invalid")
    source_clean = payload.get("source_clean")
    source_consistent = payload.get("source_consistent")
    started_at = _string(payload, "started_at", "quality_manifest_invalid")
    completed_at = _string(payload, "completed_at", "quality_manifest_invalid")
    manifest_started = _timestamp(payload, "started_at", "quality_manifest_invalid")
    manifest_completed = _timestamp(payload, "completed_at", "quality_manifest_invalid")
    if manifest_completed < manifest_started:
        raise QualityBundleError("quality_manifest_invalid")
    if COMMIT.fullmatch(commit) is None:
        raise QualityBundleError("quality_manifest_invalid")
    if COMMIT.fullmatch(expected_commit) is None or commit != expected_commit.lower():
        raise QualityBundleError("quality_commit_mismatch")
    if invocation_id != expected_invocation_id:
        raise QualityBundleError("quality_invocation_mismatch")
    if run_id != expected_run_id:
        raise QualityBundleError("quality_run_mismatch")
    if SHA256.fullmatch(expected_source_digest) is None or source_digest != expected_source_digest:
        raise QualityBundleError("quality_source_mismatch")
    if (
        not isinstance(source_clean, bool)
        or not isinstance(source_consistent, bool)
        or SHA256.fullmatch(source_digest) is None
        or SHA256.fullmatch(ending_source_digest) is None
    ):
        raise QualityBundleError("quality_manifest_invalid")
    if not source_consistent or ending_source_digest != source_digest:
        raise QualityBundleError("quality_source_changed")
    if require_clean and not source_clean:
        raise QualityBundleError("quality_source_not_clean")
    if run_id != _run_id(commit, invocation_id, source_digest):
        raise QualityBundleError("quality_run_mismatch")
    bundle = path.parent
    if (
        bundle.name != run_id
        or bundle.parent.name != "runs"
        or bundle.is_symlink()
        or bundle.parent.is_symlink()
    ):
        raise QualityBundleError("quality_run_mismatch")
    bundle_resolved = bundle.resolve()
    if expected_root is not None:
        root_resolved = expected_root.resolve()
        if not bundle_resolved.is_relative_to(root_resolved / "runs"):
            raise QualityBundleError("quality_run_mismatch")
        if _quality_validation.path_has_symlink(
            root_resolved, bundle_resolved.relative_to(root_resolved)
        ):
            raise QualityBundleError("quality_run_mismatch")
    if source_probe is None and bundle_resolved.is_relative_to(ROOT.resolve()):
        source_probe = _git_source_identity
    if source_probe is not None:
        try:
            current_source = source_probe()
        except (QualityBundleError, OSError, RuntimeError) as error:
            raise QualityBundleError("quality_source_changed") from error
        if current_source != SourceIdentity(commit, source_digest, source_clean):
            raise QualityBundleError("quality_source_changed")
    expected_files: set[Path] = {Path("manifest.json")}
    required = payload.get("required_producers")
    producers = payload.get("producers")
    if (
        not isinstance(required, list)
        or not required
        or not all(isinstance(name, str) and NAME.fullmatch(name) for name in required)
        or len(set(required)) != len(required)
        or not isinstance(producers, list)
    ):
        raise QualityBundleError("quality_manifest_invalid")
    spec_source = expected_specs if expected_specs is not None else tuple(PRODUCER_SPECS.values())
    specs_by_name = {spec.name: spec for spec in spec_source}
    if len(specs_by_name) != len(spec_source) or not set(required).issubset(specs_by_name):
        raise QualityBundleError("quality_producer_invalid")
    producer_evidence: list[ProducerEvidence] = []
    seen_producers: set[str] = set()
    for raw_producer in producers:
        if not isinstance(raw_producer, dict):
            raise QualityBundleError("quality_producer_invalid")
        name = _string(raw_producer, "name", "quality_producer_invalid")
        if name in seen_producers or name not in required:
            raise QualityBundleError("quality_producer_duplicate")
        seen_producers.add(name)
        outcome_value = raw_producer.get("outcome")
        verification_value = raw_producer.get("verification")
        if (
            not isinstance(outcome_value, str)
            or outcome_value not in {"passed", "failed"}
            or not isinstance(verification_value, str)
            or verification_value
            not in {
                "verified",
                "not_verified",
            }
        ):
            raise QualityBundleError("quality_producer_invalid")
        outcome = cast(Literal["passed", "failed"], outcome_value)
        verification = cast(Literal["verified", "not_verified"], verification_value)
        if (outcome == "passed") != (verification == "verified"):
            raise QualityBundleError("quality_producer_invalid")
        producer_started = _timestamp(raw_producer, "started_at", "quality_producer_invalid")
        producer_ended = _timestamp(raw_producer, "ended_at", "quality_producer_invalid")
        commands = raw_producer.get("commands")
        tool_versions = raw_producer.get("tool_versions")
        producer_spec = specs_by_name[name]
        if (
            producer_ended < producer_started
            or producer_started < manifest_started
            or producer_ended > manifest_completed
            or not isinstance(commands, list)
            or not commands
            or not isinstance(tool_versions, dict)
            or not tool_versions
            or not all(
                isinstance(tool, str) and tool and isinstance(version, str) and version
                for tool, version in tool_versions.items()
            )
        ):
            raise QualityBundleError("quality_producer_invalid")
        if len(commands) != len(producer_spec.commands):
            raise QualityBundleError("quality_command_mismatch")
        validated_commands = tuple(
            _validate_command_contract(command, expected)
            for command, expected in zip(commands, producer_spec.commands, strict=True)
        )
        command_exit_code = (
            validated_commands[0].exit_code if len(validated_commands) == 1 else None
        )
        expected_branch = _quality_validation.expected_branch_coverage(producer_spec.commands)
        coverage_threshold = _quality_validation.expected_coverage_threshold(producer_spec.commands)
        build_command = _quality_validation.expected_build_command(producer_spec.commands)
        if any(
            command.started_at < producer_started or command.ended_at > producer_ended
            for command in validated_commands
        ):
            raise QualityBundleError("quality_producer_invalid")
        derived_versions: dict[str, str] = {}
        for command in validated_commands:
            existing_version = derived_versions.setdefault(command.tool, command.tool_version)
            if existing_version != command.tool_version:
                raise QualityBundleError("quality_producer_invalid")
        if tool_versions != derived_versions:
            raise QualityBundleError("quality_producer_invalid")
        commands_passed = all(command.outcome == "passed" for command in validated_commands)
        if verification == "verified" and not commands_passed:
            raise QualityBundleError("quality_producer_invalid")
        raw_artifacts = raw_producer.get("artifacts")
        if not isinstance(raw_artifacts, list):
            raise QualityBundleError("quality_producer_invalid")
        artifact_specs = {
            artifact.key: artifact
            for artifact in (
                *producer_spec.artifacts,
                ArtifactSpec("result", "result.json", "json"),
            )
        }
        if len(artifact_specs) != len(producer_spec.artifacts) + 1:
            raise QualityBundleError("quality_producer_invalid")
        if not all(
            isinstance(artifact, dict) and isinstance(artifact.get("key"), str)
            for artifact in raw_artifacts
        ):
            raise QualityBundleError("quality_artifact_invalid")
        declared_keys = {artifact["key"] for artifact in raw_artifacts}
        expected_keys = set(artifact_specs)
        if "result" not in declared_keys or not declared_keys.issubset(expected_keys):
            raise QualityBundleError("quality_producer_incomplete")
        if verification == "verified" and declared_keys != expected_keys:
            raise QualityBundleError("quality_producer_incomplete")
        artifacts: list[ArtifactEvidence] = []
        seen_artifacts: set[str] = set()
        for raw_artifact in raw_artifacts:
            if not isinstance(raw_artifact, dict):
                raise QualityBundleError("quality_artifact_invalid")
            key = _string(raw_artifact, "key", "quality_artifact_invalid")
            relative_text = _string(raw_artifact, "path", "quality_artifact_invalid")
            digest = _string(raw_artifact, "sha256", "quality_artifact_invalid")
            parser = _string(raw_artifact, "format", "quality_artifact_invalid")
            verified = raw_artifact.get("verified")
            expected_artifact = artifact_specs.get(key)
            if (
                key in seen_artifacts
                or expected_artifact is None
                or not isinstance(verified, bool)
                or SHA256.fullmatch(digest) is None
                or parser not in {"json", "nonempty", "pstats"}
            ):
                raise QualityBundleError("quality_artifact_invalid")
            seen_artifacts.add(key)
            relative = Path(relative_text)
            expected_relative = Path("producers") / name / expected_artifact.relative_path
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative != expected_relative
                or parser != expected_artifact.parser
                or _quality_validation.path_has_symlink(bundle, relative)
            ):
                raise QualityBundleError("quality_artifact_path_invalid")
            artifact_path = bundle / relative
            if (
                artifact_path.is_symlink()
                or not artifact_path.is_file()
                or not artifact_path.resolve().is_relative_to(bundle_resolved)
            ):
                raise QualityBundleError("quality_artifact_missing")
            snapshot = _artifact_snapshot(artifact_path)
            observed_digest = hashlib.sha256(snapshot.data).hexdigest()
            if observed_digest != digest:
                raise QualityBundleError("quality_artifact_digest_mismatch")
            artifact_payload = _parse_artifact_bytes(snapshot.data, parser)
            _assert_artifact_unchanged(artifact_path, snapshot)
            if parser == "json" and key != "result":
                if artifact_payload is None:
                    raise QualityBundleError("quality_artifact_invalid")
                _validate_artifact_payload(
                    name,
                    key,
                    artifact_payload,
                    expected_runs=_quality_validation.expected_runs(producer_spec.commands),
                    expected_exit_code=(
                        command_exit_code if key in {"tests", "stability", "build"} else None
                    ),
                    expected_branch=expected_branch,
                    coverage_threshold=coverage_threshold,
                    build_command=build_command,
                )
            if verified != (verification == "verified"):
                raise QualityBundleError("quality_artifact_verification_mismatch")
            expected_files.add(relative)
            artifacts.append(
                ArtifactEvidence(key, artifact_path, digest, verified, artifact_payload)
            )
        producer_fact_path = bundle / "producers" / name / "producer.json"
        producer_fact_snapshot = _artifact_snapshot(producer_fact_path)
        producer_fact = _parse_artifact_bytes(producer_fact_snapshot.data, "json")
        _assert_artifact_unchanged(producer_fact_path, producer_fact_snapshot)
        if producer_fact != raw_producer:
            raise QualityBundleError("quality_producer_invalid")
        expected_files.add(Path("producers") / name / "producer.json")
        result_evidence = next(artifact for artifact in artifacts if artifact.key == "result")
        result_payload = result_evidence.payload
        expected_exit_code = next(
            (command.exit_code for command in validated_commands if command.exit_code != 0),
            0 if verification == "verified" else 1,
        )
        if (
            not isinstance(result_payload, dict)
            or result_payload.get("producer") != name
            or result_payload.get("outcome") != outcome
            or result_payload.get("verification") != verification
            or result_payload.get("started_at") != raw_producer.get("started_at")
            or result_payload.get("ended_at") != raw_producer.get("ended_at")
            or result_payload.get("tool_versions") != tool_versions
            or result_payload.get("exit_code") != expected_exit_code
        ):
            raise QualityBundleError("quality_producer_invalid")
        if verification == "verified" and any(
            _quality_validation.finding_count(artifact.key, artifact.payload or {})
            for artifact in artifacts
            if artifact.key != "result" and artifact.payload is not None
        ):
            raise QualityBundleError("quality_producer_invalid")
        producer_evidence.append(ProducerEvidence(name, outcome, verification, tuple(artifacts)))
    if seen_producers != set(required):
        raise QualityBundleError("quality_producer_incomplete")
    _quality_validation.validate_bundle_inventory(bundle, expected_files)
    return QualityManifest(
        path,
        commit,
        invocation_id,
        run_id,
        started_at,
        completed_at,
        source_digest,
        ending_source_digest,
        source_clean,
        tuple(producer_evidence),
    )


def enforce_manifest(
    manifest: QualityManifest,
    *,
    required_producers: Sequence[str],
) -> int:
    required = tuple(required_producers)
    if len(set(required)) != len(required):
        raise QualityBundleError("quality_producer_invalid")
    producers = {producer.name: producer for producer in manifest.producers}
    if set(producers) != set(required):
        return 1
    return (
        0
        if all(
            producers[name].outcome == "passed" and producers[name].verification == "verified"
            for name in required
        )
        else 1
    )


def load_run_result(path: Path, *, expected_root: Path | None = None) -> RunResult:
    if path.is_symlink() or not path.is_file():
        raise QualityBundleError("quality_result_missing")
    payload = _json_object(path, "quality_result_invalid")
    bundle_path = Path(_string(payload, "bundle", "quality_result_invalid"))
    manifest_path = Path(_string(payload, "manifest", "quality_result_invalid"))
    commit = _string(payload, "commit", "quality_result_invalid").lower()
    invocation_id = _string(payload, "invocation_id", "quality_result_invalid")
    run_id = _string(payload, "run_id", "quality_result_invalid")
    source_digest = _string(payload, "source_digest", "quality_result_invalid")
    source_clean = payload.get("source_clean")
    passed = payload.get("passed")
    exit_code = payload.get("exit_code")
    if (
        COMMIT.fullmatch(commit) is None
        or SHA256.fullmatch(source_digest) is None
        or not isinstance(source_clean, bool)
        or not isinstance(passed, bool)
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or bundle_path.name != run_id
        or bundle_path.parent.name != "runs"
        or manifest_path != bundle_path / "manifest.json"
    ):
        raise QualityBundleError("quality_result_invalid")
    if expected_root is not None:
        root_resolved = expected_root.resolve()
        bundle_resolved = bundle_path.resolve()
        if not bundle_resolved.is_relative_to(root_resolved / "runs"):
            raise QualityBundleError("quality_run_mismatch")
        if _quality_validation.path_has_symlink(
            root_resolved, bundle_resolved.relative_to(root_resolved)
        ):
            raise QualityBundleError("quality_run_mismatch")
    return RunResult(
        path,
        bundle_path,
        manifest_path,
        commit,
        invocation_id,
        run_id,
        source_digest,
        source_clean,
        passed,
        exit_code,
    )


def load_manifest_from_result(
    result: RunResult,
    *,
    require_clean: bool = False,
    expected_root: Path | None = None,
    source_probe: Callable[[], SourceIdentity] | None = None,
) -> QualityManifest:
    manifest = load_completed_manifest(
        result.manifest_path,
        expected_commit=result.commit,
        expected_invocation_id=result.invocation_id,
        expected_run_id=result.run_id,
        expected_source_digest=result.source_digest,
        require_clean=require_clean,
        expected_root=expected_root,
        source_probe=source_probe,
    )
    if manifest.source_clean != result.source_clean:
        raise QualityBundleError("quality_result_mismatch")
    manifest_passed = (
        enforce_manifest(
            manifest,
            required_producers=tuple(producer.name for producer in manifest.producers),
        )
        == 0
    )
    if manifest_passed != result.passed or (result.exit_code == 0) != result.passed:
        raise QualityBundleError("quality_result_mismatch")
    return manifest


def _build_spec() -> ProducerSpec:
    return ProducerSpec(
        name="build",
        commands=(
            CommandSpec(
                argv=(
                    sys.executable,
                    "scripts/build_metrics.py",
                    "--output",
                    f"{OUTPUT_DIRECTORY}/build.json",
                ),
                tool="npm",
                version_argv=("npm", "--version"),
            ),
        ),
        artifacts=(ArtifactSpec("build", "build.json", "json"),),
    )


def _coverage_spec() -> ProducerSpec:
    return ProducerSpec(
        name="coverage",
        commands=(
            CommandSpec(
                argv=(
                    "uv",
                    "run",
                    "pytest",
                    "tests",
                    "-m",
                    "not installer_crash_matrix",
                    "--durations=0",
                    "--cov=herdr_orchestrator",
                    "--cov-branch",
                    "--cov-report=term-missing",
                    f"--cov-report=json:{OUTPUT_DIRECTORY}/coverage.json",
                    "--cov-fail-under=80",
                    "--json-report",
                    f"--json-report-file={OUTPUT_DIRECTORY}/tests.json",
                ),
                tool="pytest",
                version_argv=("uv", "run", "pytest", "--version"),
            ),
        ),
        artifacts=(
            ArtifactSpec("coverage", "coverage.json", "json"),
            ArtifactSpec("tests", "tests.json", "json"),
        ),
    )


def _security_spec() -> ProducerSpec:
    return ProducerSpec(
        name="security",
        commands=(
            CommandSpec(
                argv=(
                    "uv",
                    "run",
                    "detect-secrets-hook",
                    "--baseline",
                    ".secrets.baseline",
                    "--",
                ),
                tool="detect-secrets",
                version_argv=("uv", "run", "detect-secrets", "--version"),
                include_tracked_files=True,
            ),
            CommandSpec(
                argv=(
                    "uv",
                    "run",
                    "bandit",
                    "-q",
                    "-r",
                    "src",
                    "-ll",
                    "-f",
                    "json",
                    "-o",
                    f"{OUTPUT_DIRECTORY}/bandit.json",
                ),
                tool="bandit",
                version_argv=("uv", "run", "bandit", "--version"),
            ),
            CommandSpec(
                argv=(
                    "uv",
                    "run",
                    "pip-audit",
                    "--local",
                    "--skip-editable",
                    "--format",
                    "json",
                    "--output",
                    f"{OUTPUT_DIRECTORY}/pip-audit.json",
                ),
                tool="pip-audit",
                version_argv=("uv", "run", "pip-audit", "--version"),
            ),
            CommandSpec(
                argv=("npm", "audit", "--package-lock-only", "--json"),
                tool="npm",
                version_argv=("npm", "--version"),
                stdout_artifact="npm-audit-root.json",
            ),
            CommandSpec(
                argv=(
                    "npm",
                    "audit",
                    "--package-lock-only",
                    "--prefix",
                    "packages/herdr-manager",
                    "--json",
                ),
                tool="npm",
                version_argv=("npm", "--version"),
                stdout_artifact="npm-audit-manager.json",
            ),
        ),
        artifacts=(
            ArtifactSpec("bandit", "bandit.json", "json"),
            ArtifactSpec("pip-audit", "pip-audit.json", "json"),
            ArtifactSpec("npm-audit-root", "npm-audit-root.json", "json"),
            ArtifactSpec("npm-audit-manager", "npm-audit-manager.json", "json"),
        ),
    )


def _lint_spec() -> ProducerSpec:
    commands = (
        CommandSpec(
            ("uv", "run", "ruff", "check", "src", "tests", "scripts"),
            "ruff",
            ("uv", "run", "ruff", "--version"),
        ),
        CommandSpec(
            ("uv", "run", "black", "--check", "src", "tests", "scripts"),
            "black",
            ("uv", "run", "black", "--version"),
        ),
        CommandSpec(("uv", "run", "mypy"), "mypy", ("uv", "run", "mypy", "--version")),
        CommandSpec(
            (
                "uv",
                "run",
                "pylint",
                "src/herdr_orchestrator",
                "--disable=all",
                "--enable=invalid-name,duplicate-code",
            ),
            "pylint",
            ("uv", "run", "pylint", "--version"),
        ),
        CommandSpec(
            ("uv", "run", "vulture", "src", "tests", "scripts", "--min-confidence", "90"),
            "vulture",
            ("uv", "run", "vulture", "--version"),
        ),
        CommandSpec(
            (
                "uv",
                "run",
                "xenon",
                "--max-absolute",
                "C",
                "--max-modules",
                "B",
                "--max-average",
                "A",
                "src",
            ),
            "xenon",
            ("uv", "run", "xenon", "--version"),
        ),
        CommandSpec(
            ("uv", "run", "lint-imports"),
            "lint-imports",
            ("uv", "run", "lint-imports", "--version"),
        ),
        CommandSpec(
            ("uv", "run", "deptry", "src"),
            "deptry",
            ("uv", "run", "deptry", "--version"),
        ),
        *(
            CommandSpec(
                ("uv", "run", "python", script, *arguments),
                "python",
                ("uv", "run", "python", "--version"),
            )
            for script, arguments in (
                ("scripts/check_repository.py", ()),
                ("scripts/check_feature_flags.py", ()),
                ("scripts/check_docs.py", ()),
                ("scripts/generate_reference.py", ("--check",)),
            )
        ),
    )
    return ProducerSpec("lint", commands, ())


def _stability_spec() -> ProducerSpec:
    return ProducerSpec(
        name="stability",
        commands=(
            CommandSpec(
                (
                    "uv",
                    "run",
                    "python",
                    "scripts/test_stability.py",
                    "--runs",
                    "3",
                    "--output",
                    f"{OUTPUT_DIRECTORY}/stability.json",
                ),
                "pytest",
                ("uv", "run", "pytest", "--version"),
            ),
        ),
        artifacts=(ArtifactSpec("stability", "stability.json", "json"),),
    )


def _profiling_spec() -> ProducerSpec:
    return ProducerSpec(
        name="profiling",
        commands=(
            CommandSpec(
                (
                    "uv",
                    "run",
                    "python",
                    "scripts/profile_tests.py",
                    "--output",
                    f"{OUTPUT_DIRECTORY}/tests.pstats",
                ),
                "pytest",
                ("uv", "run", "pytest", "--version"),
            ),
        ),
        artifacts=(ArtifactSpec("profile", "tests.pstats", "pstats"),),
    )


def _test_spec() -> ProducerSpec:
    return ProducerSpec(
        name="test",
        commands=(
            CommandSpec(
                (
                    "uv",
                    "run",
                    "pytest",
                    "tests",
                    "--durations=0",
                    "--json-report",
                    f"--json-report-file={OUTPUT_DIRECTORY}/tests.json",
                ),
                "pytest",
                ("uv", "run", "pytest", "--version"),
            ),
        ),
        artifacts=(ArtifactSpec("tests", "tests.json", "json"),),
    )


FULL_PRODUCERS = ("lint", "coverage", "stability", "security", "build", "profiling")
PRODUCER_SPECS = {
    spec.name: spec
    for spec in (
        _lint_spec(),
        _coverage_spec(),
        _stability_spec(),
        _security_spec(),
        _build_spec(),
        _profiling_spec(),
        _test_spec(),
    )
}


def _git_source_identity() -> SourceIdentity:
    try:
        commit_result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        status_result = subprocess.run(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=ROOT,
            capture_output=True,
            text=False,
            check=False,
            timeout=30,
        )
        diff_result = subprocess.run(
            ("git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"),
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
        untracked_result = subprocess.run(
            ("git", "ls-files", "-z", "--others", "--exclude-standard"),
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QualityBundleError("quality_commit_unavailable") from error
    commit = commit_result.stdout.strip()
    if commit_result.returncode != 0 or COMMIT.fullmatch(commit) is None:
        raise QualityBundleError("quality_commit_unavailable")
    if status_result.returncode != 0 or diff_result.returncode != 0 or untracked_result.returncode:
        raise QualityBundleError("quality_source_status_unavailable")
    digest = hashlib.sha256()
    digest.update(b"quality-source-v1\0")
    digest.update(commit.lower().encode())
    digest.update(b"\0tracked-diff\0")
    digest.update(diff_result.stdout)
    digest.update(b"\0untracked\0")
    for raw_name in sorted(name for name in untracked_result.stdout.split(b"\0") if name):
        name = os.fsdecode(raw_name)
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise QualityBundleError("quality_source_inventory_invalid")
        source_path = ROOT / relative
        try:
            metadata = source_path.lstat()
        except OSError as error:
            raise QualityBundleError("quality_source_inventory_changed") from error
        digest.update(raw_name)
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(metadata.st_mode)).encode())
        digest.update(b"\0")
        try:
            if stat.S_ISREG(metadata.st_mode):
                with source_path.open("rb") as source_file:
                    for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif stat.S_ISLNK(metadata.st_mode):
                digest.update(os.fsencode(os.readlink(source_path)))
            else:
                raise QualityBundleError("quality_source_inventory_invalid")
        except OSError as error:
            raise QualityBundleError("quality_source_inventory_changed") from error
        digest.update(b"\0")
    return SourceIdentity(commit.lower(), digest.hexdigest(), not bool(status_result.stdout))


def _result_payload(
    bundle: CompletedBundle,
    *,
    commit: str,
    invocation_id: str,
    source: SourceIdentity,
) -> dict[str, object]:
    return {
        "bundle": str(bundle.path),
        "commit": commit,
        "exit_code": bundle.exit_code,
        "invocation_id": invocation_id,
        "manifest": str(bundle.manifest_path),
        "passed": bundle.passed,
        "run_id": bundle.path.name,
        "source_clean": source.clean,
        "source_digest": source.digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("QUALITY_EVIDENCE_ROOT", ROOT / ".orchestrator/quality")),
    )
    run_parser.add_argument("--commit")
    run_parser.add_argument("--invocation")
    selection = run_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--producer", choices=tuple(PRODUCER_SPECS))
    selection.add_argument("--all", action="store_true")
    run_parser.add_argument("--result", type=Path)
    enforce_parser = subparsers.add_parser("enforce")
    enforce_parser.add_argument("--result", type=Path, required=True)
    enforce_parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("QUALITY_EVIDENCE_ROOT", ROOT / ".orchestrator/quality")),
    )
    enforce_parser.add_argument("--require-full", action="store_true")
    enforce_parser.add_argument("--require-clean", action="store_true")
    latest_parser = subparsers.add_parser("latest-result")
    latest_parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("QUALITY_EVIDENCE_ROOT", ROOT / ".orchestrator/quality")),
    )
    args = parser.parse_args()

    try:
        if args.command == "enforce":
            result = load_run_result(args.result, expected_root=args.root)
            manifest = load_manifest_from_result(
                result,
                require_clean=args.require_clean,
                expected_root=args.root,
            )
            required = (
                FULL_PRODUCERS
                if args.require_full
                else tuple(producer.name for producer in manifest.producers)
            )
            return enforce_manifest(manifest, required_producers=required)
        if args.command == "latest-result":
            source = _git_source_identity()
            candidates: list[tuple[int, Path]] = []
            results_root = args.root / "results"
            if results_root.is_dir():
                for result_path in results_root.glob("*.json"):
                    result = load_run_result(result_path, expected_root=args.root)
                    if result.commit == source.commit and result.source_digest == source.digest:
                        candidates.append((result_path.stat().st_mtime_ns, result_path))
            if not candidates:
                raise QualityBundleError("quality_result_missing")
            print(max(candidates)[1])
            return 0

        source = _git_source_identity()
        commit = args.commit.lower() if args.commit else source.commit
        if commit != source.commit:
            raise QualityBundleError("quality_commit_mismatch")
        invocation_id = args.invocation or uuid.uuid4().hex
        specs = (
            tuple(PRODUCER_SPECS[name] for name in FULL_PRODUCERS)
            if args.all
            else (PRODUCER_SPECS[args.producer],)
        )
        bundle = run_quality(
            root=args.root,
            commit=commit,
            invocation_id=invocation_id,
            specs=specs,
            source=source,
            source_probe=_git_source_identity,
            reuse_completed=True,
        )
        result_payload = _result_payload(
            bundle,
            commit=commit,
            invocation_id=invocation_id,
            source=source,
        )
        default_result = args.root / "results" / f"{bundle.path.name}.json"
        _quality_storage.publish_results(
            bundle_path=bundle.path,
            default_path=default_result,
            requested_path=args.result,
            payload=result_payload,
            write_json=_atomic_write_json,
        )
    except QualityBundleError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, shutil.Error):
        print("quality_storage_unavailable", file=sys.stderr)
        return 2
    except RuntimeError:
        print("quality_runtime_failure", file=sys.stderr)
        return 2
    print(bundle.manifest_path)
    return bundle.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
