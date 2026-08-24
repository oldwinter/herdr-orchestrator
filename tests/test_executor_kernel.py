from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from herdr_orchestrator.executor_kernel import (
    DependencyError,
    ExecutionKernel,
    ReplicaCapacityError,
    WorkItemConflict,
    WorkItemNotFound,
)
from herdr_orchestrator.executor_store import ExecutorStore
from herdr_orchestrator.herdr import replica_slot_names
from herdr_orchestrator.model import Harness


class ExecutionKernelTests(unittest.TestCase):
    def test_ready_work_is_dependency_ordered_and_claims_share_replica_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            run_id, _ = store.create_run(
                "research",
                "research-synthesis",
                "case-1",
            )
            kernel = ExecutionKernel(
                store,
                max_parallel=3,
                replica_capacity={"grok": 2},
                replica_slots={"grok": ("slot-1", "slot-2")},
            )

            first = kernel.add_work_item(
                run_id,
                "first",
                worker="collector",
                harness="grok",
                payload={"cell": "one"},
            )
            self.assertEqual(
                [slot.slot_name for slot in kernel.replica_slots("research", harness="grok")],
                ["slot-1", "slot-2"],
            )
            second = kernel.add_work_item(
                run_id,
                "second",
                worker="collector",
                harness="grok",
                payload={"cell": "two"},
            )
            downstream = kernel.add_work_item(
                run_id,
                "downstream",
                worker="synthesizer",
                harness="grok",
                payload={"join": True},
                depends_on=("first", "second"),
            )

            self.assertEqual(
                [item.work_id for item in kernel.ready_work_items(run_id)],
                [first.work_id, second.work_id],
            )
            claims = kernel.claim_ready(run_id)
            self.assertEqual(
                [claim.work_id for claim in claims],
                ["first", "second"],
            )
            self.assertEqual(
                {claim.replica_slot for claim in claims},
                {"slot-1", "slot-2"},
            )
            self.assertEqual(kernel.claim_ready(run_id), [])
            self.assertFalse(downstream.ready)

            kernel.complete_work_item(claims[1])
            self.assertEqual(kernel.ready_work_items(run_id), [])
            kernel.complete_work_item(claims[0])
            self.assertEqual(
                [item.work_id for item in kernel.ready_work_items(run_id)],
                ["downstream"],
            )
            settled = kernel.complete_work_item(
                claims[0],
            )
            self.assertEqual(settled.state, "succeeded")

    def test_barrier_is_durable_and_releases_all_dependents_only_after_fan_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            store = ExecutorStore(path)
            run_id, _ = store.create_run("research", "research-synthesis", "barrier")
            kernel = ExecutionKernel(
                store,
                max_parallel=4,
                replica_slots={"grok": ("slot-1", "slot-2")},
            )
            kernel.add_work_items(
                run_id,
                [
                    {"work_id": "a", "worker": "collector", "harness": "grok"},
                    {"work_id": "b", "worker": "collector", "harness": "grok"},
                    {"work_id": "join", "worker": "joiner", "harness": "grok"},
                    {"work_id": "report", "worker": "joiner", "harness": "grok"},
                ],
            )
            barrier = kernel.create_barrier(
                run_id,
                "collectors-complete",
                required_work_ids=("a", "b"),
                release_work_ids=("join", "report"),
            )

            self.assertEqual(barrier.state, "pending")
            self.assertEqual(kernel.get_barrier(run_id, "collectors-complete"), barrier)
            self.assertEqual([item.work_id for item in kernel.ready_work_items(run_id)], ["a", "b"])

            claims = kernel.claim_ready(run_id)
            kernel.complete_work_item(claims[0])
            self.assertEqual(kernel.get_barrier(run_id, "collectors-complete").state, "pending")
            self.assertEqual(kernel.ready_work_items(run_id), [])

            kernel.complete_work_item(claims[1])
            released = kernel.ready_work_items(run_id)
            self.assertEqual([item.work_id for item in released], ["join", "report"])
            self.assertEqual(
                kernel.get_barrier(run_id, "collectors-complete").state,
                "succeeded",
            )
            reopened = kernel.create_barrier(
                run_id,
                "collectors-complete",
                required_work_ids=("a", "b"),
                release_work_ids=("join", "report"),
            )
            self.assertEqual(reopened, kernel.get_barrier(run_id, "collectors-complete"))

    def test_barrier_cycle_is_rejected_before_persisting_barrier_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            run_id, _ = store.create_run("research", "research-synthesis", "barrier-cycle")
            kernel = ExecutionKernel(store)
            kernel.add_work_item(run_id, "member", depends_on=())
            kernel.add_work_item(run_id, "release", depends_on=("member",))

            with self.assertRaisesRegex(DependencyError, "barrier_cycle"):
                kernel.create_barrier(
                    run_id,
                    "cycle",
                    required_work_ids=("release",),
                    release_work_ids=("member",),
                )
            self.assertEqual(kernel.list_barriers(run_id), [])

    def test_claim_capacity_is_shared_across_runs_and_readiness_order_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            first_run, _ = store.create_run("shared", "research-synthesis", "first")
            second_run, _ = store.create_run("shared", "research-synthesis", "second")
            kernel = ExecutionKernel(
                store,
                max_parallel=4,
                replica_slots={"grok": ("slot-1", "slot-2")},
            )
            kernel.add_work_item(
                first_run,
                "z-last-created",
                ordinal=20,
                worker="collector",
                harness="grok",
            )
            kernel.add_work_item(
                first_run,
                "a-first-ordinal",
                ordinal=10,
                worker="collector",
                harness="grok",
            )
            kernel.add_work_item(
                second_run,
                "other-run",
                ordinal=0,
                worker="collector",
                harness="grok",
            )

            claims = kernel.claim_ready_for_workflow("shared", limit=4)
            self.assertEqual(len(claims), 2)
            self.assertEqual(
                [(claim.run_id, claim.work_id) for claim in claims],
                [(first_run, "a-first-ordinal"), (first_run, "z-last-created")],
            )
            self.assertEqual({claim.replica_slot for claim in claims}, {"slot-1", "slot-2"})
            self.assertEqual(kernel.claim_ready_for_workflow("shared"), [])
            self.assertEqual(
                [item.work_id for item in kernel.ready_work_items(second_run)],
                ["other-run"],
            )

    def test_invalid_dependency_graph_is_rejected_without_partial_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            run_id, _ = store.create_run("research", "research-synthesis", "invalid")
            kernel = ExecutionKernel(store)
            kernel.add_work_item(run_id, "one")
            kernel.add_work_item(run_id, "two", depends_on=("one",))

            with self.assertRaisesRegex(DependencyError, "dependency_cycle"):
                kernel.add_dependency(run_id, "one", "two")
            with self.assertRaisesRegex(WorkItemNotFound, "work_item_not_found"):
                kernel.add_work_item(run_id, "missing-parent", depends_on=("unknown",))

            self.assertEqual(
                [item.work_id for item in kernel.list_work_items(run_id)],
                ["one", "two"],
            )

    def test_batch_frontier_accepts_forward_references_and_rolls_back_invalid_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            run_id, _ = store.create_run("research", "research-synthesis", "batch")
            kernel = ExecutionKernel(store)

            created = kernel.add_work_items(
                run_id,
                [
                    {"work_id": "synthesis", "depends_on": ["second"]},
                    {"work_id": "first"},
                    {"work_id": "second", "depends_on": ["first"]},
                ],
            )
            self.assertEqual([item.work_id for item in created], ["synthesis", "first", "second"])
            self.assertEqual(
                [item.work_id for item in kernel.ready_work_items(run_id)],
                ["first"],
            )

            with self.assertRaisesRegex(WorkItemNotFound, "work_item_not_found"):
                kernel.add_work_items(
                    run_id,
                    [
                        {"work_id": "partial-a"},
                        {"work_id": "partial-b", "depends_on": ["missing"]},
                    ],
                )
            self.assertEqual(
                {item.work_id for item in kernel.list_work_items(run_id)},
                {"synthesis", "first", "second"},
            )
            with self.assertRaisesRegex(DependencyError, "dependency_cycle"):
                kernel.add_work_items(
                    run_id,
                    [
                        {"work_id": "cycle-a", "depends_on": ["cycle-b"]},
                        {"work_id": "cycle-b", "depends_on": ["cycle-a"]},
                    ],
                )
            self.assertNotIn("cycle-a", {item.work_id for item in kernel.list_work_items(run_id)})

    def test_failed_dependency_skips_descendants_and_releases_replica_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            run_id, _ = store.create_run("research", "research-synthesis", "failure")
            kernel = ExecutionKernel(
                store,
                replica_slots={"grok": ("slot-1",)},
            )
            parent = kernel.add_work_item(run_id, "parent", harness="grok")
            child = kernel.add_work_item(
                run_id,
                "child",
                harness="grok",
                depends_on=(parent.work_id,),
            )
            claim = kernel.claim_ready(run_id)[0]
            kernel.fail_work_item(claim, error_code="transport_failed")

            current_child = kernel.get_work_item(run_id, child.work_id)
            self.assertEqual(current_child.state, "skipped")
            self.assertEqual(current_child.error_code, "dependency_failed")
            self.assertEqual(kernel.ready_work_items(run_id), [])
            self.assertTrue(kernel.replica_slots("research", harness="grok")[0].available)

    def test_work_dependencies_and_slots_survive_a_fresh_kernel_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            store = ExecutorStore(path)
            run_id, _ = store.create_run("research", "research-synthesis", "restart")
            first_kernel = ExecutionKernel(
                store,
                replica_slots={"claude": ("stable-slot",)},
            )
            first_kernel.add_work_item(run_id, "source", harness="claude")
            first_kernel.add_work_item(
                run_id,
                "verify",
                harness="claude",
                depends_on=("source",),
            )
            first_claim = first_kernel.claim_ready(run_id)[0]

            restarted = ExecutionKernel(
                ExecutorStore(path),
                replica_slots={"claude": ("stable-slot",)},
            )
            self.assertEqual(restarted.ready_work_items(run_id), [])
            self.assertEqual(restarted.get_work_item(run_id, "source").state, "running")
            self.assertEqual(
                restarted.replica_slots("research", harness="claude")[0].slot_name,
                "stable-slot",
            )
            restarted.complete_work_item(first_claim)
            self.assertEqual(
                [item.work_id for item in restarted.ready_work_items(run_id)],
                ["verify"],
            )

    def test_paused_and_terminal_runs_are_not_claimable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            paused, _ = store.create_run(
                "shared",
                "research-synthesis",
                "paused",
                state="paused",
            )
            finished, _ = store.create_run(
                "shared",
                "research-synthesis",
                "finished",
                state="succeeded",
            )
            kernel = ExecutionKernel(store)
            kernel.add_work_item(paused, "paused-work")
            kernel.add_work_item(finished, "finished-work")

            self.assertEqual(kernel.claim_ready(paused), [])
            self.assertEqual(kernel.claim_ready_for_workflow("shared"), [])
            self.assertEqual(kernel.get_work_item(paused, "paused-work").state, "pending")
            self.assertEqual(kernel.get_work_item(finished, "finished-work").state, "pending")

    def test_generated_slots_keep_the_v1_stable_identity_algorithm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            store = ExecutorStore(workspace / "state.db")
            run_id, _ = store.create_run("stable", "research-synthesis", "slots")
            kernel = ExecutionKernel(
                store,
                workspace=workspace,
                replica_capacity={"grok": 2},
            )
            kernel.add_work_item(run_id, "one", harness="grok")
            kernel.add_work_item(run_id, "two", harness="grok")

            kernel.claim_ready(run_id)
            actual = tuple(
                slot.slot_name for slot in kernel.replica_slots("stable", harness="grok")
            )
            expected = replica_slot_names("stable", workspace, Harness.GROK, 2)
            self.assertEqual(actual, expected)

    def test_replica_capacity_cannot_change_for_an_existing_workflow_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            run_id, _ = store.create_run("stable", "research-synthesis", "capacity")
            kernel = ExecutionKernel(store, replica_slots={"grok": ("slot-1", "slot-2")})
            kernel.add_work_item(run_id, "one", harness="grok")
            kernel.claim_ready(run_id)

            conflicting = ExecutionKernel(store, replica_capacity={"grok": 1})
            with self.assertRaisesRegex(ReplicaCapacityError, "replica_capacity_conflict"):
                conflicting.add_work_item(run_id, "two", harness="grok")
            self.assertEqual(
                [item.work_id for item in kernel.list_work_items(run_id)],
                ["one"],
            )

    def test_concurrent_kernel_claims_cannot_oversubscribe_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            store = ExecutorStore(path)
            run_id, _ = store.create_run("shared", "research-synthesis", "concurrent")
            setup = ExecutionKernel(
                store,
                max_parallel=4,
                replica_slots={"grok": ("slot-1", "slot-2")},
            )
            setup.add_work_items(
                run_id,
                [{"work_id": f"work-{index}", "harness": "grok"} for index in range(4)],
            )

            def claim() -> list[str]:
                kernel = ExecutionKernel(
                    ExecutorStore(path),
                    max_parallel=4,
                    replica_slots={"grok": ("slot-1", "slot-2")},
                )
                return [item.work_id for item in kernel.claim_ready(run_id, limit=2)]

            with ThreadPoolExecutor(max_workers=2) as executor:
                claimed = list(executor.map(lambda _: claim(), range(2)))

            self.assertEqual(sum(len(items) for items in claimed), 2)
            self.assertEqual(len({work_id for items in claimed for work_id in items}), 2)
            self.assertEqual(
                {
                    slot.slot_name
                    for slot in setup.replica_slots("shared", harness="grok")
                    if not slot.available
                },
                {"slot-1", "slot-2"},
            )

    def test_work_creation_is_idempotent_for_same_definition_and_conflicts_on_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            run_id, _ = store.create_run("research", "research-synthesis", "dedupe")
            kernel = ExecutionKernel(store)
            first = kernel.add_work_item(
                run_id,
                "same",
                worker="collector",
                harness="grok",
                payload={"input": "pinned"},
            )
            repeated = kernel.add_work_item(
                run_id,
                "same",
                worker="collector",
                harness="grok",
                payload={"input": "pinned"},
            )
            self.assertEqual(repeated, first)
            with self.assertRaisesRegex(WorkItemConflict, "work_item_conflict"):
                kernel.add_work_item(
                    run_id,
                    "same",
                    worker="different-worker",
                    harness="grok",
                    payload={"input": "pinned"},
                )
            with self.assertRaisesRegex(WorkItemConflict, "work_item_conflict"):
                kernel.add_work_item(
                    run_id,
                    "same",
                    worker="collector",
                    harness="grok",
                    payload={"input": "pinned"},
                    ordinal=99,
                )
            self.assertEqual(len(kernel.list_work_items(run_id)), 1)

    def test_work_specification_mapping_is_exact_key_and_alias_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            run_id, _ = store.create_run("research", "research-synthesis", "mapping")
            kernel = ExecutionKernel(store)
            kernel.add_work_item(
                run_id,
                {
                    "id": "source",
                    "worker": "collector",
                    "harness": "grok",
                    "payload": {"kind": "source"},
                },
            )
            dependent = kernel.add_work_item(
                run_id,
                {
                    "id": "verify",
                    "dependencies": ["source"],
                    "worker": "verifier",
                    "harness": "claude",
                },
            )
            self.assertEqual(dependent.dependencies, ("source",))
            with self.assertRaisesRegex(WorkItemConflict, "unknown_field"):
                kernel.add_work_item(run_id, {"id": "bad", "command": ["echo", "no"]})
            with self.assertRaisesRegex(WorkItemConflict, "duplicate_id_argument"):
                kernel.add_work_item(run_id, {"id": "bad", "work_id": "also-bad"})
            with self.assertRaisesRegex(DependencyError, "duplicate_barrier_argument"):
                kernel.add_work_item(
                    run_id,
                    "bad-barrier-alias",
                    barrier_ids=("barrier",),
                    depends_on_barriers=("barrier",),
                )


if __name__ == "__main__":
    unittest.main()
