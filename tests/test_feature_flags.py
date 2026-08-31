from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from herdr_orchestrator.feature_flags import (
    FeatureFlag,
    FeatureFlagError,
    declared_flags,
    enabled,
)

_CHECKER_SPEC = importlib.util.spec_from_file_location(
    "check_feature_flags",
    Path(__file__).parents[1] / "scripts" / "check_feature_flags.py",
)
assert _CHECKER_SPEC is not None and _CHECKER_SPEC.loader is not None
checker = importlib.util.module_from_spec(_CHECKER_SPEC)
_CHECKER_SPEC.loader.exec_module(checker)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE ", "YeS"])
def test_feature_flag_true_spellings(value: str) -> None:
    assert enabled(FeatureFlag.SENTRY_EXPORT, {"HERDR_FEATURE_SENTRY_EXPORT": value})


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", " FALSE ", "OfF"])
def test_feature_flag_false_spellings(value: str) -> None:
    assert not enabled(FeatureFlag.SENTRY_EXPORT, {"HERDR_FEATURE_SENTRY_EXPORT": value})


def test_feature_flags_are_complete_unique_and_disabled_by_default() -> None:
    declarations = declared_flags()

    assert set(declarations) == set(FeatureFlag)
    assert len(set(declarations.values())) == len(declarations)
    assert all(not enabled(flag, {}) for flag in FeatureFlag)


def test_feature_flag_invalid_value_has_stable_error() -> None:
    with pytest.raises(
        FeatureFlagError,
        match=r"^feature_flag_invalid: HERDR_FEATURE_WEBHOOK_ALERTS$",
    ):
        enabled(FeatureFlag.WEBHOOK_ALERTS, {"HERDR_FEATURE_WEBHOOK_ALERTS": "maybe"})


@pytest.mark.parametrize("flag", ["sentry_export", "removed", None])
def test_feature_flag_unknown_input_has_stable_error(flag: Any) -> None:
    with pytest.raises(FeatureFlagError, match=r"^feature_flag_unknown:"):
        enabled(flag, {})


@pytest.mark.parametrize("value", [None, 1, True])
def test_feature_flag_non_string_value_has_stable_error(value: Any) -> None:
    with pytest.raises(
        FeatureFlagError,
        match=r"^feature_flag_invalid: HERDR_FEATURE_SENTRY_EXPORT$",
    ):
        enabled(FeatureFlag.SENTRY_EXPORT, {"HERDR_FEATURE_SENTRY_EXPORT": value})


def test_feature_flag_non_mapping_environment_has_stable_error() -> None:
    environ: Any = []

    with pytest.raises(FeatureFlagError, match=r"^feature_flag_environment_invalid$"):
        enabled(FeatureFlag.SENTRY_EXPORT, environ)


def _write_policy_fixture(root: Path) -> None:
    source = root / "src" / "herdr_orchestrator"
    tests = root / "tests"
    docs = root / "docs"
    source.mkdir(parents=True)
    tests.mkdir()
    docs.mkdir()
    references = "\n".join(f"    FeatureFlag.{flag.name}," for flag in FeatureFlag)
    python = (
        "from herdr_orchestrator.feature_flags import FeatureFlag\n\n"
        "REFERENCED_FLAGS = (\n"
        f"{references}\n"
        ")\n"
    )
    (source / "consumer.py").write_text(python, encoding="utf-8")
    (tests / "test_consumers.py").write_text(python, encoding="utf-8")
    lifecycle_rows = "\n".join(
        f"| `{flag.value}` | owner | purpose | 2026-01-01 | 2026-02-01 | remove |"
        for flag in FeatureFlag
    )
    (docs / "feature-flags.md").write_text(
        "| Flag | Owner | Introduced | Review by | Exit condition |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"{lifecycle_rows}\n",
        encoding="utf-8",
    )
    variables = list(declared_flags().values())
    (root / ".env.example").write_text(
        "".join(f"{variable}=false\n" for variable in variables),
        encoding="utf-8",
    )
    (docs / "observability.md").write_text(
        "".join(f"| `{variable}` | optional exporter |\n" for variable in variables),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("relative_path", "old", "new", "expected"),
    [
        (
            "src/herdr_orchestrator/consumer.py",
            "    FeatureFlag.SENTRY_EXPORT,",
            "    # FeatureFlag.SENTRY_EXPORT,",
            "sentry_export: no production consumer",
        ),
        (
            "tests/test_consumers.py",
            "    FeatureFlag.POSTHOG_ANALYTICS,",
            "    # FeatureFlag.POSTHOG_ANALYTICS,",
            "posthog_analytics: no test reference",
        ),
        (
            "docs/feature-flags.md",
            "| `webhook_alerts` | owner | purpose | 2026-01-01 | 2026-02-01 | remove |",
            "The `webhook_alerts` flag remains mentioned here.",
            "webhook_alerts: missing lifecycle row",
        ),
        (
            ".env.example",
            "HERDR_FEATURE_SENTRY_EXPORT=false",
            "# HERDR_FEATURE_SENTRY_EXPORT=false",
            "sentry_export: HERDR_FEATURE_SENTRY_EXPORT missing from .env.example",
        ),
        (
            "docs/observability.md",
            "| `HERDR_FEATURE_WEBHOOK_ALERTS` | optional exporter |",
            "The HERDR_FEATURE_WEBHOOK_ALERTS variable is documented in prose.",
            "webhook_alerts: HERDR_FEATURE_WEBHOOK_ALERTS missing from docs/observability.md",
        ),
    ],
)
def test_feature_flag_checker_requires_structured_evidence(
    tmp_path: Path,
    relative_path: str,
    old: str,
    new: str,
    expected: str,
) -> None:
    _write_policy_fixture(tmp_path)
    path = tmp_path / relative_path
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    assert expected in checker.policy_failures(tmp_path)


def test_feature_flag_checker_rejects_undeclared_python_reference(tmp_path: Path) -> None:
    _write_policy_fixture(tmp_path)
    consumer = tmp_path / "src" / "herdr_orchestrator" / "consumer.py"
    consumer.write_text(
        consumer.read_text(encoding="utf-8") + "REMOVED = FeatureFlag.REMOVED\n",
        encoding="utf-8",
    )

    assert "REMOVED: production reference has no declared flag" in checker.policy_failures(tmp_path)


def test_feature_flag_checker_rejects_retired_references(tmp_path: Path) -> None:
    _write_policy_fixture(tmp_path)
    tests = tmp_path / "tests" / "test_consumers.py"
    tests.write_text(
        tests.read_text(encoding="utf-8") + "REMOVED = FeatureFlag.REMOVED\n",
        encoding="utf-8",
    )
    with (tmp_path / ".env.example").open("a", encoding="utf-8") as handle:
        handle.write("HERDR_FEATURE_REMOVED=false\n")
    with (tmp_path / "docs" / "observability.md").open("a", encoding="utf-8") as handle:
        handle.write("| `HERDR_FEATURE_REMOVED` | retired |\n")

    failures = checker.policy_failures(tmp_path)
    assert "REMOVED: test reference has no declared flag" in failures
    assert "HERDR_FEATURE_REMOVED: .env.example entry has no declared flag" in failures
    assert "HERDR_FEATURE_REMOVED: observability entry has no declared flag" in failures


def test_feature_flag_checker_rejects_incomplete_or_duplicate_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_policy_fixture(tmp_path)
    declarations = dict(declared_flags())
    declarations.pop(FeatureFlag.WEBHOOK_ALERTS)
    declarations[FeatureFlag.POSTHOG_ANALYTICS] = declarations[FeatureFlag.SENTRY_EXPORT]
    monkeypatch.setattr(checker, "declared_flags", lambda: declarations)

    failures = checker.policy_failures(tmp_path)
    assert "webhook_alerts: missing environment mapping" in failures
    assert "HERDR_FEATURE_SENTRY_EXPORT: environment mapping is duplicated" in failures


def test_feature_flag_checker_ignores_local_class_and_string_mentions(tmp_path: Path) -> None:
    _write_policy_fixture(tmp_path)
    consumer = tmp_path / "src" / "herdr_orchestrator" / "consumer.py"
    consumer.write_text(
        "class FeatureFlag:\n    SENTRY_EXPORT = 'local'\n\n"
        "MENTION = 'FeatureFlag.POSTHOG_ANALYTICS'\n",
        encoding="utf-8",
    )

    failures = checker.policy_failures(tmp_path)
    assert "sentry_export: no production consumer" in failures
    assert "posthog_analytics: no production consumer" in failures


def test_feature_flag_checker_accepts_complete_structured_policy(tmp_path: Path) -> None:
    _write_policy_fixture(tmp_path)

    assert checker.policy_failures(tmp_path) == []


def test_feature_flag_checker_requires_false_environment_defaults(tmp_path: Path) -> None:
    _write_policy_fixture(tmp_path)
    example = tmp_path / ".env.example"
    example.write_text(
        example.read_text(encoding="utf-8").replace(
            "HERDR_FEATURE_SENTRY_EXPORT=false",
            "HERDR_FEATURE_SENTRY_EXPORT=true",
        ),
        encoding="utf-8",
    )

    assert (
        "sentry_export: HERDR_FEATURE_SENTRY_EXPORT must default to false"
        in checker.policy_failures(tmp_path)
    )
