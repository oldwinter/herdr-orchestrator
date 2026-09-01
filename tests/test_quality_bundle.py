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
            first_spec = self._coverage_spec(81.5)
            second_spec = self._coverage_spec(94.25)
            first = quality_bundle.run_quality(
                root=quality_root,
                commit=first_commit,
                invocation_id="fixture-1",
                specs=(first_spec,),
            )
            second = quality_bundle.run_quality(
                root=quality_root,
                commit=second_commit,
                invocation_id="fixture-2",
                specs=(second_spec,),
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
                json.loads(first_artifact.read_text(encoding="utf-8"))["totals"]["percent_covered"],
                81.5,
            )
            self.assertEqual(
                json.loads(second_artifact.read_text(encoding="utf-8"))["totals"][
                    "percent_covered"
                ],
                94.25,
            )
            self.assertEqual(list((quality_root / ".pending").iterdir()), [])

    def test_completed_manifest_rejects_an_artifact_digest_mismatch(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            spec = self._coverage_spec(88.0)
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="digest-mismatch",
                specs=(spec,),
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
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_successful_artifactless_producer_publishes_a_verified_result_fact(self) -> None:
        commit = "d" * 40
        with tempfile.TemporaryDirectory() as temporary:
            spec = quality_bundle.ProducerSpec(
                name="lint",
                commands=(
                    quality_bundle.CommandSpec(
                        argv=(sys.executable, "-c", "raise SystemExit(0)"),
                        tool="python",
                        version_argv=(sys.executable, "--version"),
                    ),
                ),
                artifacts=(),
            )
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="lint-result",
                specs=(spec,),
            )

            manifest = quality_bundle.load_completed_manifest(
                bundle.manifest_path,
                expected_specs=(spec,),
                **_expectations(bundle.manifest_path, expected_commit=commit),
            )

        self.assertTrue(bundle.passed)
        self.assertEqual(manifest.producers[0].verification, "verified")
        self.assertEqual(manifest.producers[0].artifacts[0].key, "result")
        self.assertEqual(manifest.producers[0].artifacts[0].payload["outcome"], "passed")

    def test_completed_manifest_rejects_incomplete_command_provenance(self) -> None:
        commit = "e" * 40
        with tempfile.TemporaryDirectory() as temporary:
            spec = self._coverage_spec(90.0)
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="missing-tool-version",
                specs=(spec,),
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
                    expected_specs=(spec,),
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
            spec = self._coverage_spec(92.0)
            bundle = quality_bundle.run_quality(
                root=root / "quality",
                commit=commit,
                invocation_id="artifact-matrix",
                specs=(spec,),
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
                                expected_specs=(spec,),
                                **_expectations(
                                    case_bundle / "manifest.json",
                                    expected_commit=commit,
                                ),
                            )

    def test_manifest_rejects_duplicate_keys_and_missing_required_producer(self) -> None:
        commit = "8" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self._coverage_spec(90.0)
            duplicate = quality_bundle.run_quality(
                root=root / "duplicate",
                commit=commit,
                invocation_id="duplicate-key",
                specs=(spec,),
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
                    expected_specs=(spec,),
                    **duplicate_expectations,
                )

            incomplete = quality_bundle.run_quality(
                root=root / "incomplete",
                commit=commit,
                invocation_id="missing-producer",
                specs=(spec,),
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
                    expected_specs=(spec,),
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
            (bundle.path / "producers" / "profiling" / "producer.json").write_text(
                json.dumps(payload["producers"][0]), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_artifact_invalid",
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_nonstandard_json_constants_are_rejected_after_digest_resync(self) -> None:
        commit = "a" * 40
        spec = self._coverage_spec(90.0)
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="nan-coverage",
                specs=(spec,),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            producer = manifest["producers"][0]
            artifact = next(item for item in producer["artifacts"] if item["key"] == "coverage")
            path = bundle.path / artifact["path"]
            data = path.read_bytes().replace(b"90.0", b"NaN", 1)
            path.write_bytes(data)
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
            (bundle.path / "producers" / "coverage" / "producer.json").write_text(
                json.dumps(producer), encoding="utf-8"
            )
            bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError, "quality_artifact_invalid"
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_fuzzed_profile_bytes_are_normalized_to_quality_error(self) -> None:
        commit = "b" * 40
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
                invocation_id="profile-fuzz",
                specs=(spec,),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            producer = manifest["producers"][0]
            artifact = next(item for item in producer["artifacts"] if item["key"] == "profile")
            path = bundle.path / artifact["path"]
            data = b"9" * 9731
            path.write_bytes(data)
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
            (bundle.path / "producers" / "profiling" / "producer.json").write_text(
                json.dumps(producer), encoding="utf-8"
            )
            bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError, "quality_artifact_invalid"
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_source_change_during_run_cannot_publish_verified_evidence(self) -> None:
        commit = "4" * 40
        initial = quality_bundle.SourceIdentity(commit, "1" * 64, False)
        changed = quality_bundle.SourceIdentity(commit, "2" * 64, False)
        probes = iter((changed,))
        with tempfile.TemporaryDirectory() as temporary:
            spec = self._coverage_spec(90.0)
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="source-mutated",
                specs=(spec,),
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
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_completed_manifest_rejects_tampered_plan_and_source_end(self) -> None:
        commit = "b" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self._coverage_spec(94.0)
            bundle = quality_bundle.run_quality(
                root=root / "base",
                commit=commit,
                invocation_id="plan-contract",
                specs=(spec,),
            )
            cases = {
                "command-count": lambda payload: payload["producers"][0]["commands"].append(
                    dict(payload["producers"][0]["commands"][0])
                ),
                "command-argv": lambda payload: payload["producers"][0]["commands"][0][
                    "argv"
                ].__setitem__(2, "forged-script"),
                "command-tool": lambda payload: payload["producers"][0]["commands"][0].__setitem__(
                    "tool", "forged-tool"
                ),
                "command-input": lambda payload: payload["producers"][0]["commands"][0].__setitem__(
                    "input_count", 1
                ),
                "ending-source": lambda payload: payload.__setitem__(
                    "ending_source_digest", "0" * 64
                ),
            }
            for label, mutate in cases.items():
                with self.subTest(label=label):
                    case = root / label / "runs" / bundle.path.name
                    case.parent.mkdir(parents=True)
                    shutil.copytree(bundle.path, case)
                    payload = json.loads((case / "manifest.json").read_text(encoding="utf-8"))
                    mutate(payload)
                    (case / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(
                        quality_bundle.QualityBundleError,
                        "quality_(?:command_mismatch|source_changed)",
                    ):
                        quality_bundle.load_completed_manifest(
                            case / "manifest.json",
                            expected_specs=(spec,),
                            **_expectations(case / "manifest.json", expected_commit=commit),
                        )

            unchanged = root / "unchanged" / "runs" / bundle.path.name
            unchanged.parent.mkdir(parents=True)
            shutil.copytree(bundle.path, unchanged)
            payload = json.loads((unchanged / "manifest.json").read_text(encoding="utf-8"))
            payload.pop("ending_source_digest")
            (unchanged / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_manifest_invalid",
            ):
                quality_bundle.load_completed_manifest(
                    unchanged / "manifest.json",
                    expected_specs=(spec,),
                    **_expectations(unchanged / "manifest.json", expected_commit=commit),
                )

    def test_default_manifest_loader_binds_the_builtin_producer_plan(self) -> None:
        commit = "f" * 40
        spec = quality_bundle.ProducerSpec(
            name="lint",
            commands=(
                quality_bundle.CommandSpec(
                    argv=(sys.executable, "-c", "raise SystemExit(0)"),
                    tool="python",
                    version_argv=(sys.executable, "--version"),
                ),
            ),
            artifacts=(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="builtin-plan",
                specs=(spec,),
            )
            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_command_mismatch",
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_completed_manifest_rejects_undeclared_bundle_files(self) -> None:
        commit = "c" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self._coverage_spec(94.0)
            bundle = quality_bundle.run_quality(
                root=root / "quality",
                commit=commit,
                invocation_id="extra-file",
                specs=(spec,),
            )
            extra = bundle.path / "producers" / "coverage" / "undeclared-secret.txt"
            extra.write_text("secret", encoding="utf-8")
            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_bundle_inventory_invalid",
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_detect_secrets_terminates_options_before_inventory_names(self) -> None:
        command = quality_bundle._security_spec().commands[0]
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(
                quality_bundle,
                "_tracked_files",
                return_value=("--exclude-files=^", "AGENTS.md"),
            ),
        ):
            invocation = quality_bundle._expanded_argv(command, Path(temporary))

        self.assertEqual(
            invocation.argv[-3:],
            ("--", "--exclude-files=^", "AGENTS.md"),
        )

    def test_completed_manifest_binds_tracked_inventory_identity(self) -> None:
        commit = "e" * 40
        spec = quality_bundle.ProducerSpec(
            name="security",
            commands=(
                quality_bundle.CommandSpec(
                    argv=(sys.executable, "-c", "raise SystemExit(0)", "--"),
                    tool="python",
                    version_argv=(sys.executable, "--version"),
                    include_tracked_files=True,
                ),
            ),
            artifacts=(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="inventory-contract",
                specs=(spec,),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            manifest["producers"][0]["commands"][0]["input_sha256"] = "0" * 64
            bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError,
                "quality_command_mismatch",
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_schema_invalid_empty_quality_reports_are_not_verified(self) -> None:
        commit = "d" * 40

        def json_spec(name: str, files: dict[str, object], artifacts: tuple[str, ...]):
            script = (
                "import json, os; from pathlib import Path; "
                "root = Path(os.environ['QUALITY_OUTPUT_DIR']); "
                + "; ".join(
                    f"(root / {filename!r}).write_text(json.dumps({payload!r}))"
                    for filename, payload in files.items()
                )
                + "; raise SystemExit(0)"
            )
            return quality_bundle.ProducerSpec(
                name=name,
                commands=(
                    quality_bundle.CommandSpec(
                        argv=(sys.executable, "-c", script),
                        tool="python",
                        version_argv=(sys.executable, "--version"),
                    ),
                ),
                artifacts=tuple(
                    quality_bundle.ArtifactSpec(key, filename, "json")
                    for key, filename in artifacts
                ),
            )

        cases = (
            (
                "empty-tests",
                json_spec(
                    "coverage",
                    {"coverage.json": {"totals": {"percent_covered": 90}}, "tests.json": {}},
                    (("coverage", "coverage.json"), ("tests", "tests.json")),
                ),
            ),
            (
                "empty-npm-audit",
                json_spec("security", {"npm.json": {}}, (("npm-audit-root", "npm.json"),)),
            ),
            (
                "empty-test-producer",
                json_spec("test", {"tests.json": {}}, (("tests", "tests.json"),)),
            ),
        )
        for label, spec in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                bundle = quality_bundle.run_quality(
                    root=Path(temporary) / "quality",
                    commit=commit,
                    invocation_id=label,
                    specs=(spec,),
                )
                self.assertFalse(bundle.passed)
                manifest = quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )
                self.assertEqual(manifest.producers[0].verification, "not_verified")
                self.assertEqual(
                    quality_bundle.enforce_manifest(manifest, required_producers=(spec.name,)),
                    1,
                )

    def test_artifact_replacement_between_digest_and_parse_is_rejected(self) -> None:
        commit = "3" * 40
        with tempfile.TemporaryDirectory() as temporary:
            spec = self._coverage_spec(93.0)
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="artifact-swap",
                specs=(spec,),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            artifact = self._artifact_path(bundle.path, manifest, "coverage")
            original_parser = quality_bundle._parse_artifact_bytes
            parse_calls = 0

            def replace_during_parse(data: bytes, parser: str):
                nonlocal parse_calls
                parse_calls += 1
                if parse_calls == 1:
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
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_producer_publishes_the_bytes_it_parsed(self) -> None:
        commit = "2" * 40
        spec = self._coverage_spec(94.0)
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
                specs=(spec,),
            )
            manifest = quality_bundle.load_completed_manifest(
                bundle.manifest_path,
                expected_specs=(spec,),
                **_expectations(bundle.manifest_path, expected_commit=commit),
            )

        coverage = next(
            artifact for artifact in manifest.producers[0].artifacts if artifact.key == "coverage"
        )
        self.assertEqual(coverage.payload["totals"]["percent_covered"], 94.0)

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

    def test_command_timeout_is_recorded_and_publishes_failed_result(self) -> None:
        commit = "7" * 40
        spec = quality_bundle.ProducerSpec(
            name="lint",
            commands=(
                quality_bundle.CommandSpec(
                    argv=(sys.executable, "-c", "import time; time.sleep(5)"),
                    tool="python",
                    version_argv=(sys.executable, "--version"),
                ),
            ),
            artifacts=(),
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(quality_bundle, "COMMAND_TIMEOUT_SECONDS", 0.05, create=True),
        ):
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="command-timeout",
                specs=(spec,),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            command = manifest["producers"][0]["commands"][0]
            result = next(
                artifact
                for artifact in manifest["producers"][0]["artifacts"]
                if artifact["key"] == "result"
            )
            result_payload = json.loads((bundle.path / result["path"]).read_text(encoding="utf-8"))

        self.assertFalse(bundle.passed)
        self.assertEqual(command["exit_code"], 124)
        self.assertEqual(command["error_code"], "quality_command_timeout")
        self.assertEqual(result_payload["verification"], "not_verified")

    def test_loader_recomputes_current_source_for_repo_bundle(self) -> None:
        commit = "8" * 40
        spec = self._coverage_spec(90.0)
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="source-refresh",
                specs=(spec,),
                source=quality_bundle.SourceIdentity(commit, "1" * 64, True),
            )
            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError, "quality_source_changed"
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    source_probe=lambda: quality_bundle.SourceIdentity(commit, "2" * 64, True),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_stability_run_count_is_bound_to_command_plan(self) -> None:
        commit = "9" * 40
        script = (
            "import json, os; from pathlib import Path; "
            "Path(os.environ['QUALITY_OUTPUT_DIR'], 'stability.json').write_text(json.dumps({"
            "'runs': 3, 'status': 'passed', 'unstable': [], 'executions': ["
            "{'run': 1, 'tests': 1, 'duration_seconds': 0.1, 'exit_code': 0}, "
            "{'run': 2, 'tests': 1, 'duration_seconds': 0.1, 'exit_code': 0}, "
            "{'run': 3, 'tests': 1, 'duration_seconds': 0.1, 'exit_code': 0}]}))"
        )
        spec = quality_bundle.ProducerSpec(
            name="stability",
            commands=(
                quality_bundle.CommandSpec(
                    argv=(sys.executable, "-c", script, "--runs", "3"),
                    tool="python",
                    version_argv=(sys.executable, "--version"),
                ),
            ),
            artifacts=(quality_bundle.ArtifactSpec("stability", "stability.json", "json"),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="stability-run-count",
                specs=(spec,),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            artifact = next(
                item for item in manifest["producers"][0]["artifacts"] if item["key"] == "stability"
            )
            stability_path = bundle.path / artifact["path"]
            payload = json.loads(stability_path.read_text(encoding="utf-8"))
            payload["runs"] = 2
            payload["executions"] = payload["executions"][:2]
            data = json.dumps(payload).encode()
            stability_path.write_bytes(data)
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
            producer = manifest["producers"][0]
            producer["artifacts"] = manifest["producers"][0]["artifacts"]
            producer_path = bundle.path / "producers" / "stability" / "producer.json"
            producer_path.write_text(json.dumps(producer), encoding="utf-8")
            bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError, "quality_artifact_invalid"
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

    def test_result_loader_rejects_bundle_outside_configured_root(self) -> None:
        commit = "0" * 40
        spec = self._coverage_spec(90.0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = quality_bundle.run_quality(
                root=root / "quality",
                commit=commit,
                invocation_id="foreign-root",
                specs=(spec,),
            )
            result_payload = quality_bundle._result_payload(
                bundle,
                commit=commit,
                invocation_id="foreign-root",
                source=quality_bundle.SourceIdentity(
                    commit,
                    json.loads(bundle.manifest_path.read_text())["source_digest"],
                    True,
                ),
            )
            result_path = root / "result.json"
            result_path.write_text(json.dumps(result_payload), encoding="utf-8")
            with self.assertRaisesRegex(quality_bundle.QualityBundleError, "quality_run_mismatch"):
                quality_bundle.load_run_result(result_path, expected_root=root / "other-quality")

    def test_coverage_display_and_branch_flags_are_semantically_bound(self) -> None:
        payload = {
            "meta": {
                "format": 3,
                "version": "fixture",
                "timestamp": "now",
                "branch_coverage": True,
                "show_contexts": False,
            },
            "files": {"fixture.py": {"summary": {"percent_covered": 90.0}}},
            "totals": {"percent_covered": 90.0, "percent_covered_display": "91"},
        }
        with self.assertRaisesRegex(quality_bundle.QualityBundleError, "quality_artifact_invalid"):
            quality_bundle._validate_artifact_payload(
                "coverage", "coverage", payload, expected_branch=True
            )
        payload["totals"]["percent_covered_display"] = "90"
        payload["meta"]["branch_coverage"] = False
        with self.assertRaisesRegex(quality_bundle.QualityBundleError, "quality_artifact_invalid"):
            quality_bundle._validate_artifact_payload(
                "coverage", "coverage", payload, expected_branch=True
            )

    def test_verified_security_findings_cannot_be_forged_to_zero(self) -> None:
        commit = "1" * 40
        files = {
            "bandit.json": {
                "errors": [],
                "results": [],
                "generated_at": "now",
                "metrics": {"_totals": {"SEVERITY.HIGH": 0}},
            },
            "pip.json": {
                "dependencies": [{"name": "fixture", "version": "1", "vulns": []}],
                "fixes": [],
            },
            "npm.json": {
                "auditReportVersion": 2,
                "metadata": {
                    "vulnerabilities": {
                        "info": 0,
                        "low": 0,
                        "moderate": 0,
                        "high": 0,
                        "critical": 0,
                        "total": 0,
                    },
                    "dependencies": {
                        "prod": 0,
                        "dev": 0,
                        "optional": 0,
                        "peer": 0,
                        "peerOptional": 0,
                        "total": 0,
                    },
                },
                "vulnerabilities": {},
            },
        }
        script = (
            "import json, os; from pathlib import Path; "
            "root=Path(os.environ['QUALITY_OUTPUT_DIR']); "
            + "; ".join(
                f"(root / {name!r}).write_text(json.dumps({payload!r}))"
                for name, payload in files.items()
            )
        )
        spec = quality_bundle.ProducerSpec(
            name="security",
            commands=(
                quality_bundle.CommandSpec(
                    (sys.executable, "-c", script), "python", (sys.executable, "--version")
                ),
            ),
            artifacts=(
                quality_bundle.ArtifactSpec("bandit", "bandit.json", "json"),
                quality_bundle.ArtifactSpec("pip-audit", "pip.json", "json"),
                quality_bundle.ArtifactSpec("npm-audit-root", "npm.json", "json"),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="security-finding",
                specs=(spec,),
            )
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            producer = manifest["producers"][0]
            bandit = next(item for item in producer["artifacts"] if item["key"] == "bandit")
            bandit_path = bundle.path / bandit["path"]
            bandit_payload = json.loads(bandit_path.read_text(encoding="utf-8"))
            bandit_payload["results"] = [{"issue": "forged"}]
            data = json.dumps(bandit_payload).encode()
            bandit_path.write_bytes(data)
            bandit["sha256"] = hashlib.sha256(data).hexdigest()
            producer_path = bundle.path / "producers" / "security" / "producer.json"
            producer_path.write_text(json.dumps(producer), encoding="utf-8")
            bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                quality_bundle.QualityBundleError, "quality_producer_invalid"
            ):
                quality_bundle.load_completed_manifest(
                    bundle.manifest_path,
                    expected_specs=(spec,),
                    **_expectations(bundle.manifest_path, expected_commit=commit),
                )

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

    def test_malformed_enum_types_fail_closed_without_typeerror(self) -> None:
        commit = "6" * 40
        spec = self._coverage_spec(95.0)
        with tempfile.TemporaryDirectory() as temporary:
            bundle = quality_bundle.run_quality(
                root=Path(temporary) / "quality",
                commit=commit,
                invocation_id="malformed-enums",
                specs=(spec,),
            )
            base = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            for field in ("outcome", "verification"):
                payload = json.loads(json.dumps(base))
                payload["producers"][0][field] = []
                case = Path(temporary) / field / "runs" / bundle.path.name
                case.parent.mkdir(parents=True)
                shutil.copytree(bundle.path, case)
                manifest_path = case / "manifest.json"
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(
                        quality_bundle.QualityBundleError, "quality_producer_invalid"
                    ),
                ):
                    quality_bundle.load_completed_manifest(
                        manifest_path,
                        expected_specs=(spec,),
                        **_expectations(manifest_path, expected_commit=commit),
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
        coverage = json.dumps(
            {
                "meta": {
                    "format": 3,
                    "version": "fixture",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "branch_coverage": True,
                    "show_contexts": False,
                },
                "files": {"fixture.py": {"summary": {"percent_covered": percent}}},
                "totals": {"percent_covered": percent},
            }
        )
        tests = json.dumps(
            {
                "created": 1.0,
                "duration": 0.1,
                "exitcode": 0,
                "root": "/fixture",
                "environment": {},
                "collectors": [],
                "summary": {"total": 1, "passed": 1, "collected": 1, "deselected": 0},
                "tests": [{"nodeid": "fixture", "outcome": "passed"}],
            }
        )
        script = (
            "import json, os; from pathlib import Path; "
            "root = Path(os.environ['QUALITY_OUTPUT_DIR']); "
            f"(root / 'coverage.json').write_text({coverage!r}); "
            f"(root / 'tests.json').write_text({tests!r})"
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
