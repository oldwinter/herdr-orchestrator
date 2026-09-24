from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

# Bound stdout/stderr captured from a harness transport so a runaway process
# cannot exhaust memory; oversized output fails closed as a transport error.
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class Command:
    argv: list[str]
    cwd: Path
    timeout_seconds: float | None


class TransportError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        exit_code: int | None = None,
        summary: str | None = None,
        agent_settled: bool = False,
    ) -> None:
        self.code = code
        self.exit_code = exit_code
        self.summary = summary
        self.agent_settled = agent_settled
        super().__init__(code)


def subprocess_runner(
    argv: list[str],
    *,
    cwd: str,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
        if (
            stdout.seek(0, io.SEEK_END) > MAX_OUTPUT_BYTES
            or stderr.seek(0, io.SEEK_END) > MAX_OUTPUT_BYTES
        ):
            raise TransportError("herdr_output_oversized")
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(
            argv,
            completed.returncode,
            stdout.read().decode("utf-8"),
            stderr.read().decode("utf-8"),
        )


def _run_command(
    runner: CommandRunner,
    command: Command,
) -> subprocess.CompletedProcess[str]:
    try:
        process = runner(
            command.argv,
            cwd=str(command.cwd),
            timeout=command.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TransportError("herdr_timeout") from exc
    except OSError as exc:
        raise TransportError("herdr_unavailable") from exc
    except UnicodeDecodeError as exc:
        raise TransportError("herdr_invalid_response") from exc
    if any(
        isinstance(stream, (str, bytes, bytearray)) and len(stream) > MAX_OUTPUT_BYTES
        for stream in (process.stdout, process.stderr)
    ):
        raise TransportError("herdr_output_oversized")
    return process


def run_json(
    runner: CommandRunner,
    command: Command,
) -> Mapping[str, Any]:
    process = _run_command(runner, command)
    if process.returncode != 0:
        raise TransportError(
            parse_error_code(process.stderr),
            exit_code=process.returncode,
        )
    try:
        payload = json.loads(process.stdout)
    except (TypeError, ValueError, RecursionError) as exc:
        raise TransportError("herdr_invalid_response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise TransportError("herdr_invalid_response")
    result = payload["result"]
    assert isinstance(result, dict)
    return result


def run_text(
    runner: CommandRunner,
    command: Command,
) -> str:
    process = _run_command(runner, command)
    if process.returncode != 0:
        raise TransportError(
            parse_error_code(process.stderr),
            exit_code=process.returncode,
        )
    if not isinstance(process.stdout, str):
        raise TransportError("herdr_invalid_response")
    return process.stdout


def parse_error_code(stderr: object) -> str:
    if not isinstance(stderr, (str, bytes, bytearray)):
        return "herdr_command_failed"
    try:
        payload = json.loads(stderr)
    except (TypeError, ValueError, RecursionError):
        return "herdr_command_failed"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str) and ERROR_CODE.fullmatch(code):
                return code
    return "herdr_command_failed"
