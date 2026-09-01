from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.model import Harness
from herdr_orchestrator.readiness import (
    BuildIdentity,
    ReadinessEnvironment,
    ReadinessErrorCode,
    ReadinessStatus,
    ReadinessVerification,
    collect_readiness_matrix,
    inspect_readiness_environment,
    resolve_build_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


class ReadinessMatrixTests(unittest.TestCase):
    def test_classifies_every_result_and_retries_only_retryable_failures(self) -> None:
        workflow = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        calls: Counter[Harness] = Counter()

        def probe(config: object, harness: Harness, timeout_seconds: int) -> dict[str, object]:
            del config, timeout_seconds
            calls[harness] += 1
            if harness is Harness.DROID:
                return _probe("ready", None, total=7)
            if harness is Harness.GROK:
                if calls[harness] == 1:
                    return _probe("timeout", "herdr_timeout", total=30_000)
                return _probe("ready", None, total=12)
            if harness is Harness.CODEX:
                return _probe("auth_required", "agent_auth_required")
            if harness is Harness.PI:
                return _probe("model_invalid", "agent_model_invalid")
            if harness is Harness.CLAUDE:
                return _probe("error", "agent_provider_failed")
            raise RuntimeError("credential=secret prompt=private terminal=full response=raw")

        matrix = collect_readiness_matrix(
            workflow,
            selected_harnesses=None,
            timeout_seconds=30,
            environment=_ready_environment(),
            build=BuildIdentity("a" * 40, "0.1.6"),
            probe=probe,
            clock=lambda: NOW,
        )
        payload = matrix.public_json()
        results = {item["harness"]: item for item in payload["results"]}

        self.assertEqual(
            list(results),
            ["droid", "grok", "codex", "pi", "claude", "hermes"],
        )
        self.assertEqual(calls[Harness.DROID], 1)
        self.assertEqual(calls[Harness.GROK], 2)
        self.assertEqual(calls[Harness.CODEX], 1)
        self.assertEqual(calls[Harness.PI], 1)
        self.assertEqual(calls[Harness.CLAUDE], 2)
        self.assertEqual(calls[Harness.HERMES], 2)
        self.assertEqual(results["droid"]["verification"], ReadinessVerification.VERIFIED.value)
        self.assertEqual(results["grok"]["status"], ReadinessStatus.READY.value)
        self.assertEqual(results["grok"]["attempt_count"], 2)
        self.assertEqual(results["grok"]["phase_timings_ms"], {"total": 12})
        self.assertEqual(results["codex"]["attempt_count"], 1)
        self.assertEqual(results["pi"]["attempt_count"], 1)
        self.assertEqual(results["claude"]["attempt_count"], 2)
        self.assertEqual(results["hermes"]["attempt_count"], 2)
        self.assertEqual(
            results["hermes"]["error_code"],
            ReadinessErrorCode.READINESS_PROBE_FAILED.value,
        )
        self.assertEqual(payload["verification"], ReadinessVerification.NOT_VERIFIED.value)
        self.assertEqual(payload["commit"], "a" * 40)
        self.assertEqual(payload["package_version"], "0.1.6")
        self.assertEqual(payload["workflow"], workflow.name)
        self.assertRegex(str(payload["workspace_id"]), r"^[a-f0-9]{16}$")
        serialized = json.dumps(payload, sort_keys=True)
        for forbidden in ("secret", "private", "terminal", "full response", "error_summary"):
            self.assertNotIn(forbidden, serialized)

    def test_expired_or_invalid_probe_evidence_is_not_verified(self) -> None:
        workflow = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        cases = (
            (
                _probe("ready", None, observed_at=NOW - timedelta(seconds=1)),
                ReadinessStatus.EXPIRED,
                ReadinessErrorCode.READINESS_EVIDENCE_EXPIRED,
            ),
            (
                {
                    "status": "ready",
                    "error_code": None,
                    "phase_timings_ms": {"arbitrary": 3},
                },
                ReadinessStatus.ERROR,
                ReadinessErrorCode.READINESS_RESULT_INVALID,
            ),
            (
                {"status": "ready", "error_code": None, "phase_timings_ms": {}},
                ReadinessStatus.ERROR,
                ReadinessErrorCode.READINESS_RESULT_INVALID,
            ),
            (
                {"status": "provider-private-status", "error_code": "raw-private-code"},
                ReadinessStatus.ERROR,
                ReadinessErrorCode.READINESS_RESULT_INVALID,
            ),
        )
        for raw, expected_status, expected_error in cases:
            with self.subTest(raw=raw):
                matrix = collect_readiness_matrix(
                    workflow,
                    selected_harnesses=["droid"],
                    timeout_seconds=30,
                    environment=_ready_environment(),
                    build=BuildIdentity("b" * 40, "0.1.6"),
                    probe=lambda *args, result=raw: result,
                    clock=lambda: NOW,
                )
                result = matrix.public_json()["results"][0]
                self.assertEqual(result["status"], expected_status.value)
                self.assertEqual(result["error_code"], expected_error.value)
                self.assertEqual(
                    result["verification"],
                    ReadinessVerification.NOT_VERIFIED.value,
                )
                self.assertEqual(result["attempt_count"], 1)

    def test_unavailable_environment_emits_complete_zero_attempt_matrix(self) -> None:
        workflow = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        called = False

        def probe(*args: object) -> dict[str, object]:
            nonlocal called
            called = True
            raise AssertionError(args)

        available = {harness: True for harness in Harness}
        profile_available = {harness: True for harness in Harness}
        profile_available[Harness.CODEX] = False
        matrix = collect_readiness_matrix(
            workflow,
            selected_harnesses=["droid", "codex"],
            timeout_seconds=30,
            environment=ReadinessEnvironment(
                managed_pane=False,
                executable_available=available,
                profile_available=profile_available,
            ),
            build=BuildIdentity("c" * 40, "0.1.6"),
            probe=probe,
            clock=lambda: NOW,
        )
        results = matrix.public_json()["results"]

        self.assertFalse(called)
        self.assertEqual([item["harness"] for item in results], ["droid", "codex"])
        self.assertEqual([item["attempt_count"] for item in results], [0, 0])
        self.assertEqual(
            [item["error_code"] for item in results],
            [
                ReadinessErrorCode.NOT_IN_HERDR.value,
                ReadinessErrorCode.NOT_IN_HERDR.value,
            ],
        )

    def test_missing_executable_and_profile_do_not_probe(self) -> None:
        workflow = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        available = {harness: True for harness in Harness}
        profiles = {harness: True for harness in Harness}
        available[Harness.DROID] = False
        profiles[Harness.CODEX] = False

        matrix = collect_readiness_matrix(
            workflow,
            selected_harnesses=["droid", "codex"],
            timeout_seconds=30,
            environment=ReadinessEnvironment(True, available, profiles),
            build=BuildIdentity("e" * 40, "0.1.6"),
            probe=lambda *args: self.fail(f"unexpected probe: {args}"),
            clock=lambda: NOW,
        )

        self.assertEqual(
            [item["error_code"] for item in matrix.public_json()["results"]],
            [
                ReadinessErrorCode.HARNESS_UNAVAILABLE.value,
                ReadinessErrorCode.PROFILE_UNAVAILABLE.value,
            ],
        )

    def test_ci_environment_never_runs_a_live_probe(self) -> None:
        workflow = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        environment = _ready_environment()
        environment = ReadinessEnvironment(
            environment.managed_pane,
            environment.executable_available,
            environment.profile_available,
            ci=True,
        )

        matrix = collect_readiness_matrix(
            workflow,
            selected_harnesses=["droid"],
            timeout_seconds=30,
            environment=environment,
            build=BuildIdentity("9" * 40, "0.1.6"),
            probe=lambda *args: self.fail(f"unexpected probe: {args}"),
            clock=lambda: NOW,
        )
        result = matrix.public_json()["results"][0]

        self.assertEqual(result["attempt_count"], 0)
        self.assertEqual(
            result["error_code"],
            ReadinessErrorCode.READINESS_CI_FORBIDDEN.value,
        )

    def test_retry_policy_is_exhaustive_over_public_error_codes(self) -> None:
        workflow = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        retryable = {
            "herdr_timeout": "timeout",
            "timeout": "timeout",
            "prompt_acceptance_timeout": "timeout",
            "agent_provider_failed": "error",
            "agent_turn_not_observed": "error",
            "herdr_invalid_response": "error",
            "task_receipt_missing": "error",
            "readiness_probe_failed": "error",
        }
        nonretryable = {
            "agent_auth_failed": "auth_required",
            "agent_auth_required": "auth_required",
            "agent_model_invalid": "model_invalid",
            "herdr_unavailable": "unavailable",
        }
        for error_code, status in {**retryable, **nonretryable}.items():
            with self.subTest(error_code=error_code):
                calls = 0

                def probe(
                    *args: object,
                    current_status: str = status,
                    current_error: str = error_code,
                ) -> dict[str, object]:
                    nonlocal calls
                    del args
                    calls += 1
                    return _probe(current_status, current_error)

                matrix = collect_readiness_matrix(
                    workflow,
                    selected_harnesses=["droid"],
                    timeout_seconds=30,
                    environment=_ready_environment(),
                    build=BuildIdentity("f" * 40, "0.1.6"),
                    probe=probe,
                    clock=lambda: NOW,
                )

                expected_calls = 2 if error_code in retryable else 1
                self.assertEqual(calls, expected_calls)
                self.assertEqual(
                    matrix.public_json()["results"][0]["attempt_count"],
                    expected_calls,
                )

    def test_inspects_managed_environment_and_exact_build_identity(self) -> None:
        workflow = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        paths = {"herdr": "/bin/herdr", "droid": "/bin/droid", "codex": "/bin/codex"}

        def git_runner(
            argv: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            del kwargs
            output = "1" * 40 + "\n" if "rev-parse" in argv else ""
            return subprocess.CompletedProcess(argv, 0, output, "")

        environment = inspect_readiness_environment(
            workflow,
            environ={
                "HERDR_ENV": "1",
                "HERDR_PANE_ID": "w1:p1",
                "HERDR_WORKSPACE_ID": "w1",
            },
            which=paths.get,
        )
        build = resolve_build_identity(
            workflow.workspace,
            "0.1.6",
            runner=git_runner,
        )

        self.assertTrue(environment.managed_pane)
        self.assertTrue(environment.executable_available[Harness.DROID])
        self.assertFalse(environment.executable_available[Harness.PI])
        self.assertTrue(environment.profile_available[Harness.CODEX])
        self.assertEqual(build, BuildIdentity("1" * 40, "0.1.6"))

    def test_dirty_tracked_or_untracked_source_is_not_verified(self) -> None:
        workflow = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
        for dirty_kind in ("tracked", "untracked"):
            with self.subTest(dirty_kind=dirty_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    workspace = Path(temporary)
                    tracked = workspace / "tracked.txt"
                    _git(workspace, "init", "-q")
                    tracked.write_text("original\n", encoding="utf-8")
                    _git(workspace, "add", "tracked.txt")
                    _git(
                        workspace,
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.invalid",
                        "commit",
                        "-qm",
                        "initial",
                    )
                    if dirty_kind == "tracked":
                        tracked.write_text("modified\n", encoding="utf-8")
                    else:
                        (workspace / "untracked.txt").write_text("new\n", encoding="utf-8")
                    build = resolve_build_identity(workspace, "0.1.6")

                    matrix = collect_readiness_matrix(
                        workflow,
                        selected_harnesses=["droid"],
                        timeout_seconds=30,
                        environment=_ready_environment(),
                        build=build,
                        probe=lambda *args: self.fail(f"unexpected probe: {args}"),
                        clock=lambda: NOW,
                    )
                    result = matrix.public_json()["results"][0]

                self.assertFalse(build.source_clean)
                self.assertEqual(result["attempt_count"], 0)
                self.assertEqual(
                    result["error_code"],
                    ReadinessErrorCode.READINESS_SOURCE_DIRTY.value,
                )

    def test_rejects_a_filter_outside_the_enabled_harnesses(self) -> None:
        workflow = load_workflow(REPO_ROOT / "workflows/grok-research.toml")

        with self.assertRaisesRegex(ValueError, "readiness_harness_not_enabled: codex"):
            collect_readiness_matrix(
                workflow,
                selected_harnesses=["codex"],
                timeout_seconds=30,
                environment=_ready_environment(),
                build=BuildIdentity("d" * 40, "0.1.6"),
                probe=lambda *args: _probe("ready", None),
                clock=lambda: NOW,
            )


def _probe(
    status: str,
    error_code: str | None,
    *,
    total: int = 0,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "error_code": error_code,
        "phase_timings_ms": {"total": total},
        "error_summary": "prompt=private credential=secret terminal=full response=raw",
    }
    if observed_at is not None:
        payload["observed_at"] = observed_at.isoformat().replace("+00:00", "Z")
    return payload


def _ready_environment() -> ReadinessEnvironment:
    return ReadinessEnvironment(
        managed_pane=True,
        executable_available={harness: True for harness in Harness},
        profile_available={harness: True for harness in Harness},
    )


def _git(workspace: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
