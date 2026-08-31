from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.protocol import (
    Command,
    TransportError,
    parse_error_code,
    run_json,
    run_text,
    subprocess_runner,
)


class ProtocolTests(unittest.TestCase):
    def test_returns_result_mapping(self) -> None:
        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: float | None,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"id": "x", "result": {"value": 1}}),
                "",
            )

        result = run_json(runner, Command(["herdr", "agent", "list"], Path("/tmp"), 10))

        self.assertEqual(result, {"value": 1})

    def test_parses_stable_error_code(self) -> None:
        self.assertEqual(
            parse_error_code('{"error":{"code":"agent_not_found","message":"missing"}}'),
            "agent_not_found",
        )
        self.assertEqual(parse_error_code("plain error"), "herdr_command_failed")
        self.assertEqual(parse_error_code(None), "herdr_command_failed")
        self.assertEqual(parse_error_code(b"\xff"), "herdr_command_failed")

    def test_preserves_nonzero_exit_and_normalizes_error_code_for_json_and_text(self) -> None:
        process = subprocess.CompletedProcess(
            ["herdr"],
            17,
            "",
            '{"error":{"code":"agent_not_found"}}',
        )

        for function in (run_json, run_text):
            with self.subTest(function=function.__name__):
                with self.assertRaisesRegex(TransportError, "agent_not_found") as raised:
                    function(_return_process(process), Command(["herdr"], Path("/tmp"), 10))
                self.assertEqual(raised.exception.code, "agent_not_found")
                self.assertEqual(raised.exception.exit_code, 17)

    def test_nonzero_exit_with_malformed_stderr_uses_fallback_code(self) -> None:
        process = subprocess.CompletedProcess(["herdr"], 2, "", "not json")

        for function in (run_json, run_text):
            with self.subTest(function=function.__name__):
                with self.assertRaisesRegex(TransportError, "herdr_command_failed") as raised:
                    function(_return_process(process), Command(["herdr"], Path("/tmp"), 10))
                self.assertEqual(raised.exception.exit_code, 2)

    def test_normalizes_timeout_and_os_errors_for_json_and_text(self) -> None:
        def timeout_runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: float | None,
        ) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout)

        def unavailable_runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: float | None,
        ) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("herdr")

        command = Command(["herdr"], Path("/tmp"), 0.25)
        for function in (run_json, run_text):
            with (
                self.subTest(function=function.__name__, error="timeout"),
                self.assertRaisesRegex(TransportError, "herdr_timeout"),
            ):
                function(timeout_runner, command)
            with (
                self.subTest(function=function.__name__, error="unavailable"),
                self.assertRaisesRegex(TransportError, "herdr_unavailable"),
            ):
                function(unavailable_runner, command)

    def test_passes_command_boundary_to_runner(self) -> None:
        seen: dict[str, object] = {}

        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: float | None,
        ) -> subprocess.CompletedProcess[str]:
            seen.update(argv=argv, cwd=cwd, timeout=timeout)
            return subprocess.CompletedProcess(argv, 0, '{"result":{}}', "")

        command = Command(["herdr", "agent", "list"], Path("/tmp"), 1.5)

        self.assertEqual(run_json(runner, command), {})
        self.assertEqual(seen, {"argv": command.argv, "cwd": "/tmp", "timeout": 1.5})

    def test_subprocess_runner_captures_text_without_shell(self) -> None:
        process = subprocess.CompletedProcess(["herdr"], 0, "output", "")

        with patch(
            "herdr_orchestrator.protocol.subprocess.run",
            return_value=process,
        ) as mocked_run:
            result = subprocess_runner(["herdr", "agent", "read"], cwd="/tmp", timeout=2.0)

        self.assertIs(result, process)
        mocked_run.assert_called_once_with(
            ["herdr", "agent", "read"],
            cwd="/tmp",
            timeout=2.0,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_rejects_malformed_json_text(self) -> None:
        process = subprocess.CompletedProcess(["herdr"], 0, "{not-json", "")

        with self.assertRaisesRegex(TransportError, "herdr_invalid_response"):
            run_json(_return_process(process), Command(["herdr"], Path("/tmp"), 10))

    def test_normalizes_non_text_process_output(self) -> None:
        process = subprocess.CompletedProcess(["herdr"], 0, None, "")

        with self.assertRaisesRegex(TransportError, "herdr_invalid_response"):
            run_json(_return_process(process), Command(["herdr"], Path("/tmp"), 10))
        with self.assertRaisesRegex(TransportError, "herdr_invalid_response"):
            run_text(_return_process(process), Command(["herdr"], Path("/tmp"), 10))

    def test_normalizes_runner_decode_errors(self) -> None:
        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: float | None,
        ) -> subprocess.CompletedProcess[str]:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        for function in (run_json, run_text):
            with (
                self.subTest(function=function.__name__),
                self.assertRaisesRegex(TransportError, "herdr_invalid_response"),
            ):
                function(runner, Command(["herdr"], Path("/tmp"), 10))

    def test_rejects_invalid_response(self) -> None:
        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: float | None,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with self.assertRaisesRegex(TransportError, "herdr_invalid_response"):
            run_json(runner, Command(["herdr"], Path("/tmp"), 10))

    def test_returns_raw_text_for_read_command(self) -> None:
        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: float | None,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "terminal text\n", "")

        output = run_text(runner, Command(["herdr", "agent", "read"], Path("/tmp"), 10))

        self.assertEqual(output, "terminal text\n")


def _return_process(
    process: subprocess.CompletedProcess[str],
):
    def runner(
        argv: list[str],
        *,
        cwd: str,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        return process

    return runner


if __name__ == "__main__":
    unittest.main()
