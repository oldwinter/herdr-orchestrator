from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.harness_health import (
    EligibilitySnapshot,
    HarnessHealth,
    HarnessHealthError,
    HarnessHealthRecord,
    HarnessHealthStatus,
    HealthSource,
)
from herdr_orchestrator.model import Harness
from herdr_orchestrator.selection import (
    effective_worker_harnesses,
    eligible_worker_harnesses,
    select_controller_harness,
)
from herdr_orchestrator.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


def _health_record(
    config,
    harness: Harness,
    *,
    status: HarnessHealthStatus = HarnessHealthStatus.READY,
    reason: str = "probe_ok",
    expires_at: float | None = 200.0,
) -> HarnessHealthRecord:
    return HarnessHealthRecord(
        workflow=config.name,
        workspace=str(config.workspace.resolve()),
        harness=harness,
        status=status,
        reason=reason,
        source=HealthSource.PROBE.value,
        observed_at=100.0,
        expires_at=expires_at,
        cooldown_until=None,
    )


def _health_snapshot(config, *records: HarnessHealthRecord) -> EligibilitySnapshot:
    return EligibilitySnapshot(
        workflow=config.name,
        workspace=str(config.workspace.resolve()),
        evaluated_at=100.0,
        records=tuple(records),
    )


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = replace(
            load_workflow(REPO_ROOT / "workflows/multi-harness.toml"),
            state_db=Path(self.temporary.name) / "state.db",
        )
        self.store = Store(self.config.state_db)
        self.store.initialize()
        self.health = HarnessHealth(self.store, self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_auto_controller_uses_first_installed_preferred_harness(self) -> None:
        available = {"grok", "codex"}

        selected = select_controller_harness(
            self.config,
            worker_harnesses=(Harness.GROK, Harness.CODEX),
            executable_finder=lambda command: command if command in available else None,
        )

        self.assertEqual(selected, Harness.GROK)

    def test_explicit_controller_overrides_auto_selection(self) -> None:
        selected = select_controller_harness(
            self.config,
            worker_harnesses=(Harness.GROK, Harness.CODEX),
            override=Harness.CODEX,
            executable_finder=lambda _: None,
        )

        self.assertEqual(selected, Harness.CODEX)

    def test_force_auto_overrides_configured_controller(self) -> None:
        config = replace(
            self.config,
            planner=replace(self.config.planner, harness=Harness.CLAUDE),
        )

        selected = select_controller_harness(
            config,
            worker_harnesses=(Harness.GROK, Harness.CODEX),
            force_auto=True,
            executable_finder=lambda command: command,
        )

        self.assertEqual(selected, Harness.GROK)

    def test_worker_override_rejects_unconfigured_harness(self) -> None:
        config = replace(
            self.config,
            workers=tuple(
                worker for worker in self.config.workers if worker.harness is not Harness.CLAUDE
            ),
        )

        with self.assertRaisesRegex(ValueError, "has_no_worker"):
            effective_worker_harnesses(config, (Harness.CLAUDE,))

    def test_auto_controller_without_installed_candidate_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "controller_harness_unavailable"):
            select_controller_harness(
                self.config,
                worker_harnesses=(Harness.GROK, Harness.CODEX),
                executable_finder=lambda _: None,
            )

    def test_requested_controller_passes_health_probe(self) -> None:
        selected = select_controller_harness(
            self.config,
            worker_harnesses=(Harness.GROK, Harness.CODEX),
            override=Harness.CODEX,
            health=self.health,
            readiness_probe=lambda _w, _h, _t: {"status": "ready"},
        )

        self.assertEqual(selected, Harness.CODEX)

    def test_requested_controller_with_degraded_probe_raises_health_error(self) -> None:
        with self.assertRaises(HarnessHealthError) as ctx:
            select_controller_harness(
                self.config,
                worker_harnesses=(Harness.GROK, Harness.CODEX),
                override=Harness.CODEX,
                health=self.health,
                readiness_probe=lambda _w, _h, _t: {
                    "status": "degraded",
                    "error_code": "probe_failed",
                },
            )

        self.assertEqual(ctx.exception.code, "controller_harness_unavailable")
        self.assertEqual(ctx.exception.harness, Harness.CODEX)

    def test_requested_controller_missing_from_snapshot_fails_closed(self) -> None:
        snapshot = _health_snapshot(
            self.config,
            _health_record(self.config, Harness.GROK),
        )

        with self.assertRaisesRegex(ValueError, "controller_health_snapshot_missing"):
            select_controller_harness(
                self.config,
                worker_harnesses=(Harness.GROK, Harness.CODEX),
                override=Harness.CODEX,
                health=self.health,
                health_snapshot=snapshot,
            )

    def test_requested_controller_ineligible_in_snapshot_raises_health_error(self) -> None:
        snapshot = _health_snapshot(
            self.config,
            _health_record(
                self.config,
                Harness.CODEX,
                status=HarnessHealthStatus.DEGRADED,
                reason="probe_failed",
                expires_at=None,
            ),
        )

        with self.assertRaises(HarnessHealthError) as ctx:
            select_controller_harness(
                self.config,
                worker_harnesses=(Harness.GROK, Harness.CODEX),
                override=Harness.CODEX,
                health=self.health,
                health_snapshot=snapshot,
            )

        self.assertEqual(ctx.exception.code, "controller_harness_unavailable")

    def test_requested_controller_eligible_in_snapshot_is_selected(self) -> None:
        snapshot = _health_snapshot(
            self.config,
            _health_record(self.config, Harness.CODEX),
        )

        selected = select_controller_harness(
            self.config,
            worker_harnesses=(Harness.GROK, Harness.CODEX),
            override=Harness.CODEX,
            health=self.health,
            health_snapshot=snapshot,
        )

        self.assertEqual(selected, Harness.CODEX)

    def test_auto_controller_uses_first_eligible_harness_from_snapshot(self) -> None:
        snapshot = _health_snapshot(
            self.config,
            _health_record(
                self.config,
                Harness.GROK,
                status=HarnessHealthStatus.DEGRADED,
                reason="probe_failed",
                expires_at=None,
            ),
            _health_record(self.config, Harness.CODEX),
        )

        selected = select_controller_harness(
            self.config,
            worker_harnesses=(Harness.GROK, Harness.CODEX),
            health=self.health,
            health_snapshot=snapshot,
        )

        self.assertEqual(selected, Harness.CODEX)

    def test_eligible_worker_harnesses_filters_through_snapshot(self) -> None:
        snapshot = _health_snapshot(
            self.config,
            _health_record(self.config, Harness.GROK),
            _health_record(
                self.config,
                Harness.CODEX,
                status=HarnessHealthStatus.UNAVAILABLE,
                reason="probe_failed",
                expires_at=None,
            ),
        )

        eligible = eligible_worker_harnesses(
            self.config,
            (Harness.GROK, Harness.CODEX),
            health=self.health,
            health_snapshot=snapshot,
        )

        self.assertEqual(eligible, (Harness.GROK,))


if __name__ == "__main__":
    unittest.main()
