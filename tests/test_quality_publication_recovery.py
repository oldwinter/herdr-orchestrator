from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from crash_matrix import CrashInjected, run_public_operation_crash_matrix
from test_quality_bundle import quality_bundle

COMMIT = "4" * 40
INVOCATION = "publication-recovery"
BOUNDARIES = (
    "claim_directory_created",
    "owner_published",
    "pending_published",
    "producers_created",
    "producer_published",
    "manifest_published",
    "bundle_published",
    "default_result_published",
    "requested_result_published",
)


def producer_spec():
    return quality_bundle.ProducerSpec(
        name="lint",
        commands=(
            quality_bundle.CommandSpec(
                argv=(
                    sys.executable,
                    "-c",
                    "import os; from pathlib import Path; "
                    "root=Path(os.environ['QUALITY_CRASH_PROJECT']); "
                    "f=(root/'producer-calls').open('a'); f.write('called\\n'); f.close(); "
                    "raise SystemExit(int(os.environ['QUALITY_CRASH_FAILURE']))",
                ),
                tool="python",
                version_argv=(sys.executable, "--version"),
            ),
        ),
        artifacts=(),
    )


def run_child(project: Path, target: str | None) -> int:
    root = project / "quality"
    original_replace = os.replace
    original_mkdir = os.mkdir
    original_unlink = os.unlink

    def checkpoint(boundary: str) -> None:
        if target == boundary:
            (project / "interruption").write_text(boundary, encoding="utf-8")
            os._exit(86)

    def replace(source, destination, *args, **kwargs):
        original_replace(source, destination, *args, **kwargs)
        path = Path(destination)
        if path.parent.name == ".claims" and path.name.endswith(".retired"):
            checkpoint("pending_retired")
        elif path.name == "owner.json":
            checkpoint("owner_published")
        elif path.parent.name == ".pending":
            checkpoint("pending_published")
        elif path.parent.name == "producers":
            checkpoint("producer_published")
        elif path.name == "manifest.json":
            checkpoint("manifest_published")
        elif path.parent.name == "runs":
            checkpoint("bundle_published")
        elif path.parent.name == "results":
            checkpoint("default_result_published")
        elif path == project / "result.json":
            checkpoint("requested_result_published")

    def mkdir(path, *args, **kwargs):
        original_mkdir(path, *args, **kwargs)
        if Path(path).parent.name == ".claims":
            checkpoint(
                "retired_directory_created"
                if Path(path).name.endswith(".retired")
                else "claim_directory_created"
            )
        elif Path(path).name == "producers":
            checkpoint("producers_created")

    def unlink(path, *args, **kwargs):
        original_unlink(path, *args, **kwargs)
        if Path(path).name == "owner.json":
            checkpoint("retired_owner_removed")

    with (
        patch.object(os, "replace", replace),
        patch.object(os, "mkdir", mkdir),
        patch.object(os, "unlink", unlink),
    ):
        bundle = quality_bundle.run_quality(
            root=root,
            commit=COMMIT,
            invocation_id=INVOCATION,
            specs=(producer_spec(),),
            reuse_completed=True,
        )
        manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
        payload = quality_bundle._result_payload(
            bundle,
            commit=COMMIT,
            invocation_id=INVOCATION,
            source=quality_bundle.SourceIdentity(COMMIT, manifest["source_digest"], True),
        )
        quality_bundle._quality_storage.publish_results(
            default_path=root / "results" / f"{bundle.path.name}.json",
            requested_path=project / "result.json",
            payload=payload,
            write_json=quality_bundle._atomic_write_json,
        )
    return bundle.exit_code


@dataclass
class PublicationCase:
    project: Path
    failed: bool = False
    boundary: str | None = None
    prior_calls: int = 0

    def execute(self, target: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, __file__, str(self.project), target or ""],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={
                **os.environ,
                "QUALITY_CRASH_PROJECT": str(self.project),
                "QUALITY_CRASH_FAILURE": "7" if self.failed else "0",
            },
        )


class QualityPublicationRecoveryTests(unittest.TestCase):
    def exercise(self, case: PublicationCase, target: str | None) -> None:
        case.boundary = target
        result = case.execute(target)
        if target is not None:
            self.assertEqual(result.returncode, 86, result.stderr)
            self.assertEqual((case.project / "interruption").read_text(), target)
            raise CrashInjected(target)
        self.assertEqual(result.returncode, 7 if case.failed else 0, result.stderr)

    def restart(self, case: PublicationCase) -> None:
        result = case.execute()
        self.assertEqual(result.returncode, 1 if case.failed else 0, result.stderr)

    def observe(self, case: PublicationCase) -> dict[str, object]:
        root = case.project / "quality"
        result = quality_bundle.load_run_result(case.project / "result.json", expected_root=root)
        manifest = quality_bundle.load_completed_manifest(
            result.manifest_path,
            expected_commit=COMMIT,
            expected_invocation_id=INVOCATION,
            expected_run_id=result.run_id,
            expected_source_digest=result.source_digest,
            expected_specs=(producer_spec(),),
            expected_root=root,
        )
        self.assertEqual(len(list((root / "runs").iterdir())), 1)
        self.assertEqual(list((root / ".pending").iterdir()), [])
        self.assertEqual(
            json.loads((case.project / "result.json").read_text()),
            json.loads((root / "results" / f"{result.run_id}.json").read_text()),
        )
        calls = (case.project / "producer-calls").read_text().splitlines()
        self.assertEqual(
            len(calls), case.prior_calls + (2 if case.boundary == "producer_published" else 1)
        )
        producer = manifest.producers[0]
        return {
            "run_id": manifest.run_id,
            "commit": manifest.commit,
            "verification": producer.verification,
            "outcome": producer.outcome,
            "verified_artifacts": [artifact.verified for artifact in producer.artifacts],
            "gate": quality_bundle.enforce_manifest(manifest, required_producers=("lint",)),
        }

    def test_owned_publication_boundaries_converge_after_hard_process_exit(self) -> None:
        outcomes = run_public_operation_crash_matrix(
            BOUNDARIES,
            setup=PublicationCase,
            run=self.exercise,
            restart=self.restart,
            observe=self.observe,
        )
        self.assertEqual(set(outcomes), set(BOUNDARIES))
        self.assertTrue(all(value["gate"] == 0 for value in outcomes.values()))

    def test_failed_evidence_stays_not_verified_after_publication_recovery(self) -> None:
        outcomes = run_public_operation_crash_matrix(
            BOUNDARIES[5:],
            setup=lambda root: PublicationCase(root, failed=True),
            run=self.exercise,
            restart=self.restart,
            observe=self.observe,
        )
        for value in outcomes.values():
            self.assertEqual(value["verification"], "not_verified")
            self.assertEqual(value["verified_artifacts"], [False])
            self.assertEqual(value["gate"], 1)

    def test_recovery_can_itself_crash_while_retiring_incomplete_evidence(self) -> None:
        def setup(root: Path) -> PublicationCase:
            case = PublicationCase(root, prior_calls=1)
            self.assertEqual(case.execute("producer_published").returncode, 86)
            return case

        run_public_operation_crash_matrix(
            ("retired_directory_created", "pending_retired", "retired_owner_removed"),
            setup=setup,
            run=self.exercise,
            restart=self.restart,
            observe=self.observe,
        )

    def test_legacy_ownerless_pending_data_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = PublicationCase(Path(temporary))
            self.assertEqual(case.execute("pending_published").returncode, 86)
            pending = next((case.project / "quality" / ".pending").iterdir())
            (pending / "owner.json").unlink()
            marker = pending / "unproven-data"
            marker.write_text("keep me", encoding="utf-8")
            result = case.execute()
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr.strip(), "quality_run_reused")
            self.assertEqual(marker.read_text(), "keep me")
            self.assertFalse((case.project / "producer-calls").exists())
            self.assertFalse((case.project / "result.json").exists())

    def test_live_mismatched_and_symlink_owners_are_refused_without_mutation(self) -> None:
        for state in ("live", "mismatched", "symlink"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                case = PublicationCase(Path(temporary))
                self.assertEqual(case.execute("pending_published").returncode, 86)
                pending = next((case.project / "quality" / ".pending").iterdir())
                owner = pending / "owner.json"
                payload = json.loads(owner.read_text())
                if state == "live":
                    payload["pid"] = os.getpid()
                elif state == "mismatched":
                    payload["run_id"] = "another-run"
                else:
                    foreign = case.project / "foreign-owner.json"
                    owner.rename(foreign)
                    owner.symlink_to(foreign)
                if state != "symlink":
                    owner.write_text(json.dumps(payload), encoding="utf-8")
                before = owner.read_bytes()
                result = case.execute()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr.strip(), "quality_run_reused")
                self.assertEqual(owner.read_bytes(), before)
                self.assertFalse((case.project / "producer-calls").exists())

    def test_concurrent_recovery_lock_prevents_a_second_producer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = PublicationCase(Path(temporary))
            self.assertEqual(case.execute("pending_published").returncode, 86)
            pending = next((case.project / "quality" / ".pending").iterdir())
            with quality_bundle._quality_storage.run_lock(case.project / "quality", pending.name):
                result = case.execute()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr.strip(), "quality_run_reused")
                self.assertFalse((case.project / "producer-calls").exists())
            self.restart(case)

    def test_invalid_completed_pending_evidence_is_preserved_without_rerunning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = PublicationCase(Path(temporary))
            self.assertEqual(case.execute("manifest_published").returncode, 86)
            pending = next((case.project / "quality" / ".pending").iterdir())
            manifest_path = pending / "manifest.json"
            payload = json.loads(manifest_path.read_text())
            payload["invocation_id"] = "wrong-invocation"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            result = case.execute()
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr.strip(), "quality_invocation_mismatch")
            final = case.project / "quality" / "runs" / pending.name
            self.assertEqual(json.loads((final / "manifest.json").read_text()), payload)
            self.assertEqual((case.project / "producer-calls").read_text().splitlines(), ["called"])


if __name__ == "__main__":
    try:
        raise SystemExit(run_child(Path(sys.argv[1]), sys.argv[2] or None))
    except quality_bundle.QualityBundleError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
