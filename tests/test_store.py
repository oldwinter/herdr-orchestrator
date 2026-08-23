from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
)
from herdr_orchestrator.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.db")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_enqueue_is_idempotent(self) -> None:
        job = _job("same")

        first_id, first_created = self.store.enqueue(job)
        second_id, second_created = self.store.enqueue(job)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)

    def test_claims_only_one_job_per_harness(self) -> None:
        self.store.enqueue(_job("one", Harness.CODEX))
        self.store.enqueue(_job("two", Harness.CODEX))
        self.store.enqueue(_job("three", Harness.DROID))

        claimed = self.store.claim("example", limit=3, lease_seconds=60)

        self.assertEqual(len(claimed), 2)
        self.assertEqual({job.harness for job in claimed}, {Harness.CODEX, Harness.DROID})
        self.assertEqual({job.agent_name for job in claimed}, {"ho-codex", "ho-droid"})

    def test_claims_up_to_harness_replica_slots(self) -> None:
        self.store.enqueue(_job("one", Harness.GROK))
        self.store.enqueue(_job("two", Harness.GROK))
        self.store.enqueue(_job("three", Harness.GROK))

        claimed = self.store.claim(
            "example",
            limit=5,
            lease_seconds=60,
            slot_names={"grok": ("ho-grok-01-slot", "ho-grok-02-slot")},
        )

        self.assertEqual(len(claimed), 2)
        self.assertEqual(
            [job.agent_name for job in claimed],
            ["ho-grok-01-slot", "ho-grok-02-slot"],
        )

    def test_success_records_receipt_and_terminal_state(self) -> None:
        self.store.enqueue(_job("one"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        state = self.store.record_outcome(
            claimed,
            DispatchOutcome("worker", AgentState.DONE, False, "w1:p2"),
        )

        self.assertEqual(state, JobState.SUCCEEDED)
        self.assertEqual(self.store.status_counts("example")["succeeded"], 1)

    def test_agent_blocked_error_is_terminal_blocked_state(self) -> None:
        self.store.enqueue(_job("one"))
        claimed = self.store.claim("example", limit=1, lease_seconds=60)[0]

        state = self.store.record_outcome(
            claimed,
            DispatchOutcome(
                "worker",
                AgentState.BLOCKED,
                True,
                "w1:p2",
                "agent_blocked",
            ),
        )

        self.assertEqual(state, JobState.BLOCKED)
        self.assertEqual(self.store.status_counts("example")["blocked"], 1)

    def test_failure_retries_then_exhausts_attempts(self) -> None:
        self.store.enqueue(_job("one", max_attempts=2))
        first = self.store.claim("example", limit=1, lease_seconds=60)[0]
        state = self.store.record_outcome(
            first,
            DispatchOutcome(
                "worker",
                AgentState.UNKNOWN,
                False,
                None,
                "herdr_timeout",
            ),
        )
        self.assertEqual(state, JobState.PENDING)

        with patch("herdr_orchestrator.store.time.time", return_value=time.time() + 120):
            second = self.store.claim("example", limit=1, lease_seconds=60)[0]
            state = self.store.record_outcome(
                second,
                DispatchOutcome(
                    "worker",
                    AgentState.UNKNOWN,
                    True,
                    "w1:p2",
                    "herdr_timeout",
                ),
            )

        self.assertEqual(state, JobState.FAILED)

    def test_expired_lease_is_reclaimed(self) -> None:
        self.store.enqueue(_job("one"))
        baseline = time.time() + 1
        with patch("herdr_orchestrator.store.time.time", return_value=baseline):
            first = self.store.claim("example", limit=1, lease_seconds=30)[0]
        with patch("herdr_orchestrator.store.time.time", return_value=baseline + 31):
            second = self.store.claim("example", limit=1, lease_seconds=30)[0]

        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(second.attempt, 2)


def _job(
    dedupe_key: str,
    harness: Harness = Harness.CODEX,
    *,
    max_attempts: int = 3,
) -> NewJob:
    return NewJob(
        workflow="example",
        title=dedupe_key,
        harness=harness,
        prompt="Do the task",
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )


if __name__ == "__main__":
    unittest.main()
