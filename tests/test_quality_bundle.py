from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/quality_bundle.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("quality_bundle_regression", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


quality_bundle = _load_script()


def _expectations(path: Path, *, expected_commit: str | None = None) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "expected_commit": expected_commit or payload["commit"],
        "expected_invocation_id": payload["invocation_id"],
        "expected_run_id": payload["run_id"],
        "expected_source_digest": payload["source_digest"],
    }


class QualityBundleRunTests(unittest.TestCase):
    def test_two_runs_isolate_fixed_artifact_names_by_commit_and_invocation(self) -> None:
        first_commit = "1" * 40
        second_commit = "2" * 40
        with tempfile.TemporaryDirectory() as temporary:
            quality_root = Path(temporary) / "quality"
            first = quality_bundle.run_quality(
                root=quality_root,
                commit=first_commit,
                invocation_id="fixture-1",
                specs=(self._coverage_spec(81.5),),
            )
            second = quality_bundle.run_quality(
                root=quality_root,
                commit=second_commit,
                invocation_id="fixture-2",
                specs=(self._coverage_spec(94.25),),
            )

            first_manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            second_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
            first_artifact = self._artifact_path(first.path, first_manifest, "coverage")
            second_artifact = self._artifact_path(second.path, second_manifest, "coverage")

            self.assertNotEqual(first.path, second.path)
            self.assertEqual(first_manifest["commit"], first_commit)
            self.assertEqual(first_manifest["invocation_id"], "fixture-1")
            self.assertEqual(second_manifest["commit"], second_commit)
            self.assertEqual(second_manifest["invocation_id"], "fixture-2")
            self.assertEqual(
                json.loads(first_artifact.read_text(encoding="utf-8"))["percent"], 81.5
            )
            self.assertEqual(
                json.loads(second_artifact.read_text(encoding="utf-8"))["percent"], 94.25
            )
            self.assertEqual(list((quality_root / ".pending").iterdir()), [])

    def test_completed_manifest_rejects_an_artifact_digest_mismatch(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="digest-mismatch",
                specs=(self._coverage_spec(88.0),),
            )
            payload = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            artifact = self._artifact_path(bundle.path, payload, "coverage")
            artifact.write_text('{"percent": 100.0}', encoding="utf-8")

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_artifact_digest_mismatch",
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_successful_artifactless_producer_publishes_a_verified_result_fact(self) -> None:
        commit = "d" * 40
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="lint-result",
                specs=(
                    quality_bundle.ProducerSpec(
                        name="lint",
                        commands=(
                            quality_bundle.CommandSpec(
                                argv=(sys.executable, "-c", "raise SystemExit(0)"),
                                tool="python",
                                version_argv=(sys.executable, "--version"),
                            ),
                        ),
                        artifacts=(),
                    ),
                ),
            )

            manifest = quality_bundle.load_completed_manifest(
                bundle.manifest_path,
                **_expectations(bundle.manifest_path, expected_commit=commit),
            )

        self.assertTrue(bundle.passed)
        self.assertEqual(manifest.producers[0].verification, "verified")
        self.assertEqual(manifest.producers[0].artifacts[0].key, "result")
        self.assertEqual(manifest.producers[0].artifacts[0].payload["outcome"], "passed")

    def test_completed_manifest_rejects_incomplete_command_provenance(self) -> None:
        commit = "e" * 40
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="missing-tool-version",
                specs=(self._coverage_spec(90.0),),
            )
            payload = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            del payload["producers"][0]["commands"][0]["tool_version"]
            bundle.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_producer_invalid",
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_reused_run_identity_is_rejected(self) -> None:
        commit = "f" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "quality"
            quality_bundle.run_quality(
                root=root,
                commit=commit,
                invocation_id="reused",
                specs=(self._coverage_spec(90.0),),
            )

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_run_reused",
            ):
                quality_bundle.run_quality(
                    root=root,
                    commit=commit,
                    invocation_id="reused",
                    specs=(self._coverage_spec(91.0),),
                )

    def test_each_published_artifact_is_required_and_digest_bound(self) -> None:
        commit = "9" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = quality_bundle.run_quality(
                root=root / "quality",
                commit=commit,
                invocation_id="artifact-matrix",
                specs=(self._coverage_spec(92.0),),
            )
            payload = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            artifacts = payload["producers"][0]["artifacts"]

            for index, artifact in enumerate(artifacts):
                for mode, error_code in (
                    ("missing", "quality_artifact_missing"),
                    ("corrupt", "quality_artifact_digest_mismatch"),
                ):
                    with self.subTest(key=artifact["key"], mode=mode):
                        case_bundle = root / "cases" / f"{index}-{mode}" / "runs" / bundle.path.name
                        case_bundle.parent.mkdir(parents=True)
                        shutil.copytree(bundle.path, case_bundle)
                        target = case_bundle / artifact["path"]
                        if mode == "missing":
                            target.unlink()
                        else:
                            target.write_bytes(target.read_bytes() + b"corrupt")
                        with self.assertRaisesRegex(
                            quality_bundle.QualityBundleError,
                            error_code,
                        ):
                            quality_bundle.load_completed_manifest(
                                case_bundle / "manifest.json",
                                **_expectations(
                                    case_bundle / "manifest.json",
                                    expected_commit=commit,
                                ),
                            )

    def test_manifest_rejects_duplicate_keys_and_missing_required_producer(self) -> None:
        commit = "8" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = quality_bundle.run_quality(
                root=root / "duplicate",
                commit=commit,
                invocation_id="duplicate-key",
                specs=(self._coverage_spec(90.0),),
            )
            text = duplicate.manifest_path.read_text(encoding="utf-8")
            duplicate_expectations = _expectations(
                duplicate.manifest_path,
                expected_commit=commit,
            )
            duplicate.manifest_path.write_text(
                text.replace("{", '{"schema_version": 1,', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_manifest_invalid",
            ):
                quality_bundle.load_completed_manifest(
                    duplicate.manifest_path,
                    **duplicate_expectations,
                )

            incomplete = quality_bundle.run_quality(
                root=root / "incomplete",
                commit=commit,
                invocation_id="missing-producer",
                specs=(self._coverage_spec(90.0),),
            )
            payload = json.loads(incomplete.manifest_path.read_text(encoding="utf-8"))
            payload["producers"] = []
            incomplete.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_producer_incomplete",
            ):
                quality_bundle.load_completed_manifest(
                    incomplete.manifest_path,
                    **_expectations(incomplete.manifest_path, expected_commit=commit),
                )

    def test_invalid_profile_is_recorded_as_an_unverified_producer(self) -> None:
        commit = "6" * 40
        script = (
            "import os; from pathlib import Path; "
            "Path(os.environ['QUALITY_OUTPUT_DIR'], 'tests.pstats').write_bytes(b'')"
        )
        spec = quality_bundle.ProducerSpec(
            name="profiling",
            commands=(
                quality_bundle.CommandSpec(
                    argv=(sys.executable, "-c", script),
                    tool="python",
                    version_argv=(sys.executable, "--version"),
                ),
            ),
            artifacts=(quality_bundle.ArtifactSpec("profile", "tests.pstats", "pstats"),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="profile-eof",
                specs=(spec,),
            )
            payload = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))

        self.assertFalse(bundle.passed)
        self.assertEqual(payload["producers"][0]["verification"], "not_verified")
        self.assertEqual(
            {artifact["key"] for artifact in payload["producers"][0]["artifacts"]},
            {"result"},
        )

    def test_matching_digest_does_not_make_invalid_profile_parseable(self) -> None:
        commit = "5" * 40
        script = (
            "import cProfile, os; from pathlib import Path; "
            "path = Path(os.environ['QUALITY_OUTPUT_DIR'], 'tests.pstats'); "
            "profiler = cProfile.Profile(); profiler.runcall(sum, range(10)); "
            "profiler.dump_stats(str(path))"
        )
        spec = quality_bundle.ProducerSpec(
            name="profiling",
            commands=(
                quality_bundle.CommandSpec(
                    argv=(sys.executable, "-c", script),
                    tool="python",
                    version_argv=(sys.executable, "--version"),
                ),
            ),
            artifacts=(quality_bundle.ArtifactSpec("profile", "tests.pstats", "pstats"),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="profile-semantic-corruption",
                specs=(spec,),
            )
            payload = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            artifact = next(
                item for item in payload["producers"][0]["artifacts"] if item["key"] == "profile"
            )
            profile = bundle.path / artifact["path"]
            profile.write_bytes(b"")
            artifact["sha256"] = hashlib.sha256(b"").hexdigest()
            bundle.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_artifact_invalid",
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_source_change_during_run_cannot_publish_verified_evidence(self) -> None:
        commit = "4" * 40
        initial = quality_bundle.SourceIdentity(commit, "1" * 64, False)
        changed = quality_bundle.SourceIdentity(commit, "2" * 64, False)
        probes = iter((changed,))
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="source-mutated",
                specs=(self._coverage_spec(90.0),),
                source=initial,
                source_probe=lambda: next(probes),
            )
            payload = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))

            self.assertFalse(bundle.passed)
            self.assertFalse(payload["source_consistent"])
            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_source_changed",
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_artifact_replacement_between_digest_and_parse_is_rejected(self) -> None:
        commit = "3" * 40
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="artifact-swap",
                specs=(self._coverage_spec(93.0),),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            artifact = self._artifact_path(bundle.path, manifest, "coverage")
            original_parser = quality_bundle._parse_artifact_bytes

            def replace_during_parse(data: bytes, parser: str):
                artifact.write_text('{"percent": 0}', encoding="utf-8")
                return original_parser(data, parser)

            with (
                patch.object(
                    quality_bundle,
                    "_parse_artifact_bytes",
                    side_effect=replace_during_parse,
                ),
                self.assertRaisesRegex(
                    quality_bundle.QualityBundleError,
                    "quality_artifact_changed",
                ),
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_producer_publishes_the_bytes_it_parsed(self) -> None:
        commit = "2" * 40
        original_inspector = quality_bundle._inspect_artifact

        def mutate_after_inspection(path: Path, parser: str):
            inspected = original_inspector(path, parser)
            if path.name == "coverage.json":
                path.write_text('{"percent": 0}', encoding="utf-8")
            return inspected

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(
                quality_bundle,
                "_inspect_artifact",
                side_effect=mutate_after_inspection,
            ),
        ):
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="producer-snapshot",
                specs=(self._coverage_spec(94.0),),
            )
            manifest = quality_bundle.load_completed_manifest(
                bundle.manifest_path,
                **_expectations(bundle.manifest_path, expected_commit=commit),
            )

        coverage = next(
            artifact for artifact in manifest.producers[0].artifacts if artifact.key == "coverage"
        )
        self.assertEqual(coverage.payload["percent"], 94.0)

    def test_same_run_identity_has_one_atomic_claim_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / ".pending" / "same-run"
            final = root / "runs" / "same-run"
            pending.parent.mkdir()
            final.parent.mkdir()
            barrier = Barrier(2)

            def claim() -> str:
                barrier.wait()
                try:
                    quality_bundle._claim_run_directory(pending, final)
                except quality_bundle.QualityBundleError as error:
                    return str(error)
                return "claimed"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = sorted(executor.map(lambda _: claim(), range(2)))

        self.assertEqual(outcomes, ["claimed", "quality_run_reused"])

    def test_manifest_binds_command_semantics_and_required_artifact_declarations(self) -> None:
        commit = "1" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self._coverage_spec(95.0)
            bundle = quality_bundle.run_quality(
                root=root / "base",
                commit=commit,
                invocation_id="semantic-matrix",
                specs=(spec,),
            )

            def nonzero_passed(payload: dict[str, object]) -> None:
                payload["producers"][0]["commands"][0]["exit_code"] = 7

            def tool_map_mismatch(payload: dict[str, object]) -> None:
                payload["producers"][0]["tool_versions"]["python"] = "different"

            def reversed_command_time(payload: dict[str, object]) -> None:
                payload["producers"][0]["commands"][0]["ended_at"] = "2000-01-01T00:00:00Z"

            def omit_tests_artifact(payload: dict[str, object]) -> None:
                producer = payload["producers"][0]
                producer["artifacts"] = [
                    artifact for artifact in producer["artifacts"] if artifact["key"] != "tests"
                ]

            for label, mutate, error_code in (
                ("nonzero-passed", nonzero_passed, "quality_producer_invalid"),
                ("tool-map", tool_map_mismatch, "quality_producer_invalid"),
                ("reversed-time", reversed_command_time, "quality_producer_invalid"),
                ("missing-tests", omit_tests_artifact, "quality_producer_incomplete"),
            ):
                with self.subTest(label=label):
                    case_bundle = root / label / "runs" / bundle.path.name
                    case_bundle.parent.mkdir(parents=True)
                    shutil.copytree(bundle.path, case_bundle)
                    payload = json.loads(
                        (case_bundle / "manifest.json").read_text(encoding="utf-8")
                    )
                    mutate(payload)
                    (case_bundle / "manifest.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(
                        quality_bundle.QualityBundleError,
                        error_code,
                    ):
                        quality_bundle.load_completed_manifest(
                            case_bundle / "manifest.json",
                            expected_specs=(spec,),
                            **_expectations(
                                case_bundle / "manifest.json",
                                expected_commit=commit,
                            ),
                        )

    def test_result_fact_must_match_its_producer_record(self) -> None:
        commit = "0" * 40
        spec = self._coverage_spec(96.0)
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="result-mismatch",
                specs=(spec,),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            result_artifact = next(
                artifact
                for artifact in manifest["producers"][0]["artifacts"]
                if artifact["key"] == "result"
            )
            result_path = bundle.path / result_artifact["path"]
            result_payload = json.loads(result_path.read_text(encoding="utf-8"))
            result_payload["outcome"] = "failed"
            result_data = json.dumps(result_payload).encode()
            result_path.write_bytes(result_data)
            result_artifact["sha256"] = hashlib.sha256(result_data).hexdigest()
            bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_producer_invalid",
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_enforcement_uses_manifest_outcomes_without_a_summary_file(self) -> None:
        commit = "a" * 40
        base_spec = self._coverage_spec(97.0)
        failing_command = quality_bundle.CommandSpec(
            argv=(sys.executable, "-c", "raise SystemExit(7)"),
            tool="python",
            version_argv=(sys.executable, "--version"),
        )
        spec = quality_bundle.ProducerSpec(
            base_spec.name,
            (failing_command,),
            base_spec.artifacts,
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="manifest-enforce",
                specs=(spec,),
            )
            manifest = quality_bundle.load_completed_manifest(
                bundle.manifest_path,
                expected_specs=(spec,),
                **_expectations(bundle.manifest_path, expected_commit=commit),
            )

        self.assertFalse((bundle.path / "summary.md").exists())
        self.assertEqual(
            quality_bundle.enforce_manifest(manifest, required_producers=("coverage",)),
            1,
        )

    @staticmethod
    def _coverage_spec(percent: float):
        payload = json.dumps({"percent": percent})
        script = (
            "import json, os; from pathlib import Path; "
            "root = Path(os.environ['QUALITY_OUTPUT_DIR']); "
            f"(root / 'coverage.json').write_text({payload!r}); "
            "(root / 'tests.json').write_text(json.dumps({'tests': []}))"
        )
        return quality_bundle.ProducerSpec(
            name="coverage",
            commands=(
                quality_bundle.CommandSpec(
                    argv=(sys.executable, "-c", script),
                    tool="python",
                    version_argv=(sys.executable, "--version"),
                ),
            ),
            artifacts=(
                quality_bundle.ArtifactSpec("coverage", "coverage.json", "json"),
                quality_bundle.ArtifactSpec("tests", "tests.json", "json"),
            ),
        )

    @staticmethod
    def _artifact_path(bundle: Path, manifest: dict[str, object], key: str) -> Path:
        producers = manifest["producers"]
        assert isinstance(producers, list)
        artifacts = producers[0]["artifacts"]
        artifact = next(item for item in artifacts if item["key"] == key)
        return bundle / artifact["path"]


if __name__ == "__main__":
    unittest.main()
