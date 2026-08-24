from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.executor_store import (
    CANONICALIZATION_VERSION,
    DefinitionIdentity,
    ExecutorStore,
    ManifestValidationError,
    PinnedRunManifest,
    RunDedupeConflict,
    canonical_json,
    definition_digest,
)
from herdr_orchestrator.model import Harness, NewJob
from herdr_orchestrator.store import Store


class ExecutorStoreTests(unittest.TestCase):
    def test_definition_identity_is_canonical_and_order_sensitive_for_arrays(self) -> None:
        first = {"b": 2, "a": ["one", "two"]}
        reordered = {"a": ["one", "two"], "b": 2}
        changed = {"a": ["two", "one"], "b": 2}

        self.assertEqual(canonical_json(first), '{"a":["one","two"],"b":2}')
        self.assertEqual(
            definition_digest(first),
            "sha256:9c7e0a7f85bf9405ca34040eef2d77be8f66779fcceffffd5440e0f53269ee97",
        )
        self.assertEqual(
            DefinitionIdentity.from_value(first),
            DefinitionIdentity.from_value(reordered),
        )
        self.assertNotEqual(
            DefinitionIdentity.from_value(first),
            DefinitionIdentity.from_value(changed),
        )
        self.assertEqual(
            DefinitionIdentity.from_value(first).canonicalization_version,
            CANONICALIZATION_VERSION,
        )

    def test_manifest_accepts_explicit_contract_vocabulary_aliases(self) -> None:
        manifest = PinnedRunManifest.from_digests(
            prompt_rubric_digest="1" * 64,
            source_revision_digest="2" * 64,
            executor_contract_digest="3" * 64,
            executor_implementation_digest="4" * 64,
            artifact_digest="5" * 64,
        )

        self.assertEqual(manifest.prompt_digest, "sha256:" + "1" * 64)
        self.assertEqual(manifest.source_digest, "sha256:" + "2" * 64)
        self.assertEqual(manifest.contract_digest, "sha256:" + "3" * 64)
        self.assertEqual(manifest.executor_digest, "sha256:" + "4" * 64)
        self.assertEqual(manifest.artifact_contract_digest, "sha256:" + "5" * 64)

    def test_additive_initialization_preserves_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            legacy = Store(path)
            legacy.initialize()
            legacy.enqueue(
                NewJob(
                    workflow="legacy",
                    title="unchanged",
                    harness=Harness.DROID,
                    prompt="Read only.",
                    dedupe_key="legacy-job",
                    max_attempts=2,
                )
            )
            before_counts = legacy.status_counts("legacy")
            before_jobs = legacy.jobs("legacy")

            v2 = ExecutorStore(path)
            v2.initialize()
            v2.initialize()

            self.assertEqual(legacy.status_counts("legacy"), before_counts)
            self.assertEqual(legacy.jobs("legacy"), before_jobs)
            self.assertEqual(v2.feature_version(), 1)
            self.assertEqual(
                v2.feature_versions(),
                {
                    "artifacts": 1,
                    "attempt-kernel": 1,
                    "receipt-events": 1,
                    "run-store": 1,
                    "work-kernel": 1,
                },
            )
            self.assertEqual(v2.attempt_feature_version(), 1)
            self.assertEqual(v2.receipt_feature_version(), 1)

    def test_run_persists_the_complete_pinned_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            manifest = PinnedRunManifest.from_values(
                workflow={"schema_version": 2, "name": "research"},
                config={"max_parallel": 2},
                input_value={"question": "What changed?"},
                source={"revision": "source-1"},
                route={"collector": ["grok", "claude"]},
                profile={"name": "default"},
                prompt={"collector": "prompt-v1"},
                static_checks=[],
                contract={"name": "kernel-v1"},
                executor={"kind": "research-synthesis", "version": 1},
                artifact_contract={"version": 1},
            )

            run_id, created = store.create_run(
                "research",
                "research-synthesis",
                "question-1",
                manifest,
            )

            self.assertTrue(created)
            persisted = store.require_run(run_id)
            self.assertEqual(persisted.state, "pending")
            self.assertEqual(persisted.manifest, manifest)
            self.assertEqual(
                persisted.manifest.to_dict(),
                {
                    "workflow_digest": manifest.workflow_digest,
                    "config_digest": manifest.config_digest,
                    "input_digest": manifest.input_digest,
                    "source_digest": manifest.source_digest,
                    "route_digest": manifest.route_digest,
                    "profile_digest": manifest.profile_digest,
                    "prompt_digest": manifest.prompt_digest,
                    "static_check_digest": manifest.static_check_digest,
                    "contract_digest": manifest.contract_digest,
                    "executor_digest": manifest.executor_digest,
                    "artifact_contract_digest": manifest.artifact_contract_digest,
                    "manifest_version": 1,
                    "canonicalization_version": 1,
                },
            )
            self.assertEqual(
                persisted.manifest.manifest_digest,
                definition_digest(persisted.manifest.to_dict()),
            )
            inspected = store.inspect_run(run_id)
            self.assertEqual(inspected["schema_version"], 2)
            self.assertEqual(inspected["manifest"], persisted.manifest.to_dict())
            self.assertEqual(inspected["pinned_manifest"], persisted.manifest.to_dict())
            self.assertEqual(inspected["manifest_digest"], persisted.manifest.manifest_digest)

    def test_dedupe_returns_one_identical_run_and_rejects_conflicting_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            first_manifest = PinnedRunManifest.from_values(
                workflow={"name": "example"},
                config={"profile": "one"},
                input_value={"question": "same"},
            )
            second_manifest = PinnedRunManifest.from_values(
                workflow={"name": "example"},
                config={"profile": "two"},
                input_value={"question": "same"},
            )

            first_id, first_created = store.create_run(
                "example",
                "research-synthesis",
                "same-key",
                first_manifest,
            )
            repeated_id, repeated_created = store.create_run(
                "example",
                "research-synthesis",
                "same-key",
                first_manifest,
            )

            self.assertTrue(first_created)
            self.assertFalse(repeated_created)
            self.assertEqual(repeated_id, first_id)
            before_conflict = store.require_run(first_id).to_dict()

            with self.assertRaisesRegex(RunDedupeConflict, "run_dedupe_conflict"):
                store.create_run(
                    "example",
                    "research-synthesis",
                    "same-key",
                    second_manifest,
                )

            self.assertEqual(store.require_run(first_id).to_dict(), before_conflict)
            self.assertEqual([run.run_id for run in store.runs()], [first_id])

    def test_manifest_rejects_unknown_fields_and_invalid_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            valid = PinnedRunManifest.from_values(input_value={"question": "valid"})

            with self.assertRaisesRegex(ManifestValidationError, "unknown_field"):
                store.create_run(
                    "example",
                    "research-synthesis",
                    "invalid-unknown",
                    {**valid.to_dict(), "unexpected": True},
                )
            with self.assertRaisesRegex(ManifestValidationError, "input_digest_invalid"):
                store.create_run(
                    "example",
                    "research-synthesis",
                    "invalid-digest",
                    {**valid.to_dict(), "input_digest": "not-a-digest"},
                )

            self.assertEqual(store.runs(), [])

    def test_create_run_can_pin_each_definition_without_recomputing_later(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ExecutorStore(Path(temporary) / "state.db")
            definitions = {
                "workflow": {"name": "example", "schema_version": 2},
                "config": {"max_parallel": 2},
                "input": {"question": "pin this"},
                "source": {"revision": "source-1"},
                "route": {"worker": "collector"},
                "profile": {"name": "default"},
                "prompt": {"digest_source": "prompt-1"},
                "static_checks": [{"id": "compile", "argv": ["python3", "-m", "compileall"]}],
                "contract": {"version": "executor-v1"},
                "executor": {"kind": "research-synthesis", "version": 1},
                "artifact_contract": {"version": 1},
            }

            run_id, created = store.create_run(
                "example",
                "research-synthesis",
                "direct-definitions",
                workflow_definition=definitions["workflow"],
                config_definition=definitions["config"],
                input_value=definitions["input"],
                source_definition=definitions["source"],
                route_definition=definitions["route"],
                profile_definition=definitions["profile"],
                prompt_definition=definitions["prompt"],
                static_checks=definitions["static_checks"],
                contract_definition=definitions["contract"],
                executor_definition=definitions["executor"],
                artifact_contract_definition=definitions["artifact_contract"],
            )

            persisted = store.require_run(run_id).manifest
            self.assertTrue(created)
            for field, source_key in (
                ("workflow_digest", "workflow"),
                ("config_digest", "config"),
                ("input_digest", "input"),
                ("source_digest", "source"),
                ("route_digest", "route"),
                ("profile_digest", "profile"),
                ("prompt_digest", "prompt"),
                ("static_check_digest", "static_checks"),
                ("contract_digest", "contract"),
                ("executor_digest", "executor"),
                ("artifact_contract_digest", "artifact_contract"),
            ):
                with self.subTest(field=field):
                    self.assertEqual(
                        getattr(persisted, field),
                        definition_digest(definitions[source_key]),
                    )

    def test_concurrent_identical_creates_are_serialized_by_the_dedupe_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            manifest = PinnedRunManifest.from_values(input_value={"question": "same"})

            def create() -> tuple[str, bool]:
                return ExecutorStore(path).create_run(
                    "example",
                    "research-synthesis",
                    "concurrent-key",
                    manifest,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: create(), range(2)))

            self.assertEqual({run_id for run_id, _ in results}, {results[0][0]})
            self.assertEqual(sum(created for _, created in results), 1)
            self.assertEqual(len(ExecutorStore(path).runs()), 1)


if __name__ == "__main__":
    unittest.main()
