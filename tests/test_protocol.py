from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from herdr_orchestrator.protocol import (
    Command,
    TransportError,
    parse_error_code,
    run_json,
    run_text,
)


class ProtocolTests(unittest.TestCase):
    def test_returns_result_mapping(self) -> None:
        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: int | None,
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

    def test_rejects_invalid_response(self) -> None:
        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: int | None,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with self.assertRaisesRegex(TransportError, "herdr_invalid_response"):
            run_json(runner, Command(["herdr"], Path("/tmp"), 10))

    def test_returns_raw_text_for_read_command(self) -> None:
        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: int | None,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "terminal text\n", "")

        output = run_text(runner, Command(["herdr", "agent", "read"], Path("/tmp"), 10))

        self.assertEqual(output, "terminal text\n")


if __name__ == "__main__":
    unittest.main()
