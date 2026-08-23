from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: int | None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class Command:
    argv: list[str]
    cwd: Path
    timeout_seconds: int | None


class TransportError(RuntimeError):
    def __init__(self, code: str, *, exit_code: int | None = None) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)


def subprocess_runner(
    argv: list[str],
    *,
    cwd: str,
    timeout: int | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


def run_json(
    runner: CommandRunner,
    command: Command,
) -> Mapping[str, Any]:
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
    if process.returncode != 0:
        raise TransportError(
            parse_error_code(process.stderr),
            exit_code=process.returncode,
        )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise TransportError("herdr_invalid_response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise TransportError("herdr_invalid_response")
    return payload["result"]


def run_text(
    runner: CommandRunner,
    command: Command,
) -> str:
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
    if process.returncode != 0:
        raise TransportError(
            parse_error_code(process.stderr),
            exit_code=process.returncode,
        )
    return process.stdout


def parse_error_code(stderr: str) -> str:
    try:
        payload = json.loads(stderr)
    except json.JSONDecodeError:
        return "herdr_command_failed"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str) and ERROR_CODE.fullmatch(code):
                return code
    return "herdr_command_failed"
