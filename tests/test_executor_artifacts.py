from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.executor_artifacts import (
    ArtifactError,
    ArtifactStore,
    ArtifactValidationError,
    AttemptRoots,
    digest_bytes,
)
from herdr_orchestrator.executor_protocol import MANIFEST_DIGEST_FIELDS
from herdr_orchestrator.executor_kernel import ClaimLostError, ExecutionKernel
from herdr_orchestrator.executor_store import ExecutorStore


class AttemptRootsTests(unittest.TestCase):
    def test_attempt_roots_are_exclusive_and_contained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "roots")
            kernel = ExecutionKernel(store, workspace=root, replica_slots={"droid": ("slot-1",)})
            kernel.add_work_item(
                run_id,
                "source",
                harness="droid",
                payload={"assigned_path": "result.json", "lineage": ["source"]},
            )
            claim = kernel.claim_ready(run_id)[0]

            attempt = AttemptRoots.for_claim(root / "runtime", claim)

            self.assertEqual(
                attempt.root,
                (root / "runtime").resolve()
                / run_id
                / "attempts"
                / "source"
                / f"{claim.attempt_number}-{claim.fencing_token}",
            )
            self.assertEqual(attempt.out, attempt.root / "out")
            self.assertEqual(attempt.scratch, attempt.root / "scratch")
            attempt.ensure()
            self.assertTrue(attempt.out.is_dir())
            self.assertTrue(attempt.scratch.is_dir())
            self.assertTrue(attempt.out.is_relative_to(attempt.root))
            self.assertTrue(attempt.scratch.is_relative_to(attempt.root))

    def test_assigned_output_rejects_escape_and_sibling_attempt_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "paths")
            kernel = ExecutionKernel(store, replica_slots={"droid": ("slot-1",)})
            kernel.add_work_item(run_id, "source", harness="droid")
            claim = kernel.claim_ready(run_id)[0]
            attempt = AttemptRoots.for_claim(root / "runtime", claim)
            attempt.ensure()

            self.assertEqual(attempt.assigned_output("result.json"), attempt.out / "result.json")
            with self.assertRaisesRegex(ValueError, "artifact_path"):
                attempt.assigned_output("../outside.json")
            with self.assertRaisesRegex(ValueError, "artifact_path"):
                attempt.assigned_output(str(root / "outside.json"))
            with self.assertRaisesRegex(ValueError, "artifact_path"):
                attempt.assigned_output(str(attempt.root.parent / "other" / "result.json"))

    def test_cleanup_removes_owned_attempt_tree_but_not_boundary_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            sentinel = root / "outside.txt"
            sentinel.write_text("keep", encoding="utf-8")
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "cleanup")
            kernel = ExecutionKernel(store, replica_slots={"droid": ("slot-1",)})
            kernel.add_work_item(run_id, "source", harness="droid")
            claim = kernel.claim_ready(run_id)[0]
            attempt = AttemptRoots.for_claim(runtime, claim)
            attempt.ensure()
            attempt.assigned_output("result.json").write_text("unadmitted", encoding="utf-8")
            (attempt.scratch / "intermediate.txt").write_text("scratch", encoding="utf-8")

            removed = attempt.cleanup()

            self.assertTrue(removed)
            self.assertFalse(attempt.root.exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


class ArtifactAdmissionTests(unittest.TestCase):
    def test_valid_typed_output_is_content_addressed_and_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "admit")
            kernel = ExecutionKernel(store, workspace=root, replica_slots={"droid": ("slot-1",)})
            kernel.add_work_item(
                run_id,
                "source",
                harness="droid",
                worker="collector",
                payload={"assigned_path": "result.json", "lineage": ["source"]},
            )
            claim = kernel.claim_ready(run_id)[0]
            artifacts = ArtifactStore(store, root / "runtime")
            content = b'{"ok":true}'
            output, digest, size = artifacts.stage(claim, "result.json", content)
            manifest = store.require_run(run_id).manifest
            envelope = {
                "contract_version": 1,
                "artifact_type": "source-result",
                "run_id": run_id,
                "work_id": "source",
                "logical_id": "source",
                "attempt_id": claim.attempt_id,
                "fencing_token": claim.fencing_token,
                "path": "result.json",
                "content_digest": digest,
                "size_bytes": size,
                "lineage": ["source"],
                "pinned_digests": {
                    key: getattr(manifest, key) for key in MANIFEST_DIGEST_FIELDS
                },
                "harness": "droid",
                "worker": "collector",
                "agent": claim.agent_name,
                "pane": f"pane:{claim.agent_name}",
                "payload": {"ok": True},
            }

            admitted = artifacts.admit(claim, envelope, expected_lineage=("source",))

            self.assertTrue(admitted.admitted)
            self.assertEqual(admitted.content_digest, digest)
            self.assertTrue(Path(admitted.admitted_path).is_file())
            self.assertEqual(Path(admitted.admitted_path).read_bytes(), content)
            self.assertEqual([item.artifact_id for item in artifacts.list_artifacts(run_id)], [admitted.artifact_id])
            self.assertTrue(output.is_file())

    def test_wrong_digest_is_rejected_without_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "digest")
            kernel = ExecutionKernel(store, replica_slots={"droid": ("slot-1",)})
            kernel.add_work_item(
                run_id,
                "source",
                harness="droid",
                worker="collector",
                payload={"assigned_path": "result.json", "lineage": ["source"]},
            )
            claim = kernel.claim_ready(run_id)[0]
            artifacts = ArtifactStore(store, root / "runtime")
            artifacts.stage(claim, "result.json", b"actual")
            manifest = store.require_run(run_id).manifest
            envelope = _envelope(
                claim,
                {key: getattr(manifest, key) for key in MANIFEST_DIGEST_FIELDS},
                content_digest=digest_bytes(b"expected"),
                size_bytes=len(b"expected"),
            )

            with self.assertRaisesRegex(ArtifactValidationError, "artifact_digest_mismatch"):
                artifacts.admit(claim, envelope)
            self.assertEqual(artifacts.list_artifacts(run_id), [])

    def test_inspect_detects_missing_or_corrupted_admitted_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "integrity")
            kernel = ExecutionKernel(
                store,
                runtime_dir=root / "runtime",
                replica_slots={"droid": ("slot-1",)},
            )
            kernel.add_work_item(
                run_id,
                "source",
                harness="droid",
                worker="collector",
                payload={"assigned_path": "result.json", "lineage": ["source"]},
            )
            claim = kernel.claim_ready(run_id)[0]
            _, digest, size = kernel.stage_artifact(claim, "result.json", b"durable")
            manifest = store.require_run(run_id).manifest
            admitted = kernel.complete_work_item(
                claim,
                artifact=_envelope(
                    claim,
                    {key: getattr(manifest, key) for key in MANIFEST_DIGEST_FIELDS},
                    content_digest=digest,
                    size_bytes=size,
                ),
            )
            self.assertEqual(admitted.state, "succeeded")
            artifact_path = Path(kernel.list_artifacts(run_id)[0].admitted_path)
            artifact_path.write_bytes(b"corrupted")

            with self.assertRaisesRegex(ArtifactError, "artifact_content_integrity_failed"):
                kernel.inspect_run(run_id)

    def test_child_lineage_requires_an_intact_admitted_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "lineage")
            kernel = ExecutionKernel(
                store,
                runtime_dir=root / "runtime",
                replica_slots={"droid": ("slot-1",)},
            )
            kernel.add_work_item(
                run_id,
                "parent",
                harness="droid",
                worker="collector",
                payload={"assigned_path": "result.json", "lineage": ["parent"]},
            )
            kernel.add_work_item(
                run_id,
                "child",
                harness="droid",
                worker="collector",
                payload={"assigned_path": "result.json", "lineage": ["parent"]},
            )
            manifest = store.require_run(run_id).manifest
            pinned = {key: getattr(manifest, key) for key in MANIFEST_DIGEST_FIELDS}
            parent = kernel.claim_ready(run_id)[0]
            _, parent_digest, parent_size = kernel.stage_artifact(
                parent,
                "result.json",
                b"parent",
            )
            kernel.complete_work_item(
                parent,
                artifact=_envelope(
                    parent,
                    pinned,
                    content_digest=parent_digest,
                    size_bytes=parent_size,
                ),
            )
            parent_path = Path(kernel.list_artifacts(run_id)[0].admitted_path)
            parent_path.write_bytes(b"corrupt-parent")
            child = kernel.claim_ready(run_id)[0]
            _, child_digest, child_size = kernel.stage_artifact(
                child,
                "result.json",
                b"child",
            )

            with self.assertRaisesRegex(
                ArtifactValidationError,
                "artifact_lineage_parent_integrity",
            ):
                kernel.complete_work_item(
                    child,
                    artifact=_envelope(
                        child,
                        pinned,
                        content_digest=child_digest,
                        size_bytes=child_size,
                        lineage=["parent"],
                    ),
                )

    def test_kernel_settlement_requires_admitted_artifact_when_artifact_is_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "settle")
            kernel = ExecutionKernel(
                store,
                runtime_dir=root / "runtime",
                replica_slots={"droid": ("slot-1",)},
            )
            kernel.add_work_item(
                run_id,
                "source",
                harness="droid",
                worker="collector",
                payload={"assigned_path": "result.json", "lineage": ["source"]},
            )
            claim = kernel.claim_ready(run_id)[0]
            output, digest, size = kernel.stage_artifact(claim, "result.json", b"done")
            manifest = store.require_run(run_id).manifest
            envelope = _envelope(
                claim,
                {key: getattr(manifest, key) for key in MANIFEST_DIGEST_FIELDS},
                content_digest=digest,
                size_bytes=size,
            )

            settled = kernel.complete_work_item(claim, artifact=envelope)

            self.assertEqual(settled.state, "succeeded")
            self.assertEqual(len(kernel.list_receipts(run_id)), 1)
            receipt_payload = kernel.list_receipts(run_id)[0].payload
            self.assertEqual(receipt_payload["run_id"], run_id)
            self.assertEqual(receipt_payload["work_id"], claim.work_id)
            self.assertEqual(receipt_payload["attempt_id"], claim.attempt_id)
            self.assertEqual(receipt_payload["fencing_token"], claim.fencing_token)
            self.assertEqual(receipt_payload["harness"], claim.harness)
            self.assertEqual(receipt_payload["worker"], claim.worker)
            self.assertEqual(receipt_payload["agent"], claim.agent_name)
            self.assertEqual(receipt_payload["pane"], f"pane:{claim.agent_name}")
            self.assertEqual(len(kernel.list_artifacts(run_id)), 1)
            self.assertEqual(
                [event.event_type for event in kernel.list_events(run_id)],
                ["attempt_claimed", "artifact_admitted", "work_succeeded"],
            )
            kernel.cleanup_attempt(claim)
            self.assertFalse(output.parent.parent.exists())
            admitted_path = Path(kernel.list_artifacts(run_id)[0].admitted_path)
            self.assertEqual(admitted_path.read_bytes(), b"done")

    def test_lifecycle_settlement_is_observation_only_without_typed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "lifecycle")
            kernel = ExecutionKernel(
                store,
                runtime_dir=root / "runtime",
                replica_slots={"droid": ("slot-1",)},
            )
            kernel.add_work_item(
                run_id,
                "source",
                harness="droid",
                worker="collector",
                payload={"assigned_path": "result.json", "lineage": ["source"]},
            )
            claim = kernel.claim_ready(run_id)[0]

            observation = kernel.record_lifecycle_settlement(claim, lifecycle="done")

            self.assertEqual(observation["lifecycle"], "done")
            self.assertEqual(kernel.get_work_item(run_id, "source").state, "running")
            self.assertEqual(kernel.list_receipts(run_id), [])
            self.assertEqual(kernel.list_artifacts(run_id), [])

    def test_successful_settlement_cannot_bypass_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "bypass")
            kernel = ExecutionKernel(
                store,
                runtime_dir=root / "runtime",
                replica_slots={"droid": ("slot-1",)},
            )
            kernel.add_work_item(
                run_id,
                "source",
                harness="droid",
                worker="collector",
                payload={"assigned_path": "result.json", "lineage": ["source"]},
            )
            claim = kernel.claim_ready(run_id)[0]

            with self.assertRaisesRegex(ArtifactValidationError, "artifact_required"):
                kernel.complete_work_item(claim)

            self.assertEqual(kernel.get_work_item(run_id, "source").state, "running")
            self.assertEqual(kernel.list_receipts(run_id), [])

    def test_stale_output_is_rejected_once_and_cannot_replace_current_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [100.0]
            store = ExecutorStore(root / "state.db")
            run_id, _ = store.create_run("example", "research-synthesis", "stale-artifact")
            kernel = ExecutionKernel(
                store,
                runtime_dir=root / "runtime",
                lease_seconds=10,
                clock=lambda: now[0],
                replica_slots={"droid": ("slot-1",)},
            )
            kernel.add_work_item(
                run_id,
                "source",
                harness="droid",
                worker="collector",
                payload={"assigned_path": "result.json", "lineage": ["source"]},
            )
            old = kernel.claim_ready(run_id)[0]
            _, old_digest, old_size = kernel.stage_artifact(old, "result.json", b"old")
            manifest = store.require_run(run_id).manifest
            old_envelope = _envelope(
                old,
                {key: getattr(manifest, key) for key in MANIFEST_DIGEST_FIELDS},
                content_digest=old_digest,
                size_bytes=old_size,
            )
            now[0] = 110.0
            kernel.reclaim_expired(run_id)
            current = kernel.claim_ready(run_id)[0]
            _, current_digest, current_size = kernel.stage_artifact(
                current,
                "result.json",
                b"current",
            )
            current_envelope = _envelope(
                current,
                {key: getattr(manifest, key) for key in MANIFEST_DIGEST_FIELDS},
                content_digest=current_digest,
                size_bytes=current_size,
            )
            kernel.complete_work_item(current, artifact=current_envelope)

            with self.assertRaisesRegex(ClaimLostError, "claim_lost"):
                kernel.complete_work_item(old, artifact=old_envelope)
            with self.assertRaisesRegex(ClaimLostError, "claim_lost"):
                kernel.complete_work_item(old, artifact=old_envelope)

            artifacts = kernel.list_artifacts(run_id)
            self.assertEqual(
                sorted(item.state for item in artifacts),
                ["admitted", "stale"],
            )
            self.assertEqual(len(kernel.list_receipts(run_id)), 1)
            self.assertEqual(kernel.get_work_item(run_id, "source").state, "succeeded")
            self.assertEqual(
                [event.event_type for event in kernel.list_events(run_id)].count(
                    "artifact_rejected"
                ),
                1,
            )


def _envelope(claim: object, pinned: dict[str, object], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": 1,
        "artifact_type": "result",
        "run_id": claim.run_id,
        "work_id": claim.work_id,
        "logical_id": claim.work_id,
        "attempt_id": claim.attempt_id,
        "fencing_token": claim.fencing_token,
        "path": "result.json",
        "content_digest": digest_bytes(b"actual"),
        "size_bytes": len(b"actual"),
        "lineage": [claim.work_id],
        "pinned_digests": pinned,
        "harness": claim.harness,
        "worker": claim.worker,
        "agent": claim.agent_name,
        "pane": f"pane:{claim.agent_name}",
        "payload": {},
    }
    value.update(overrides)
    return value


if __name__ == "__main__":
    unittest.main()
