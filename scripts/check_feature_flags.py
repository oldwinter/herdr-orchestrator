#!/usr/bin/env python3
"""Reject undeclared, unused, or undocumented feature flags."""

from __future__ import annotations

from pathlib import Path

from herdr_orchestrator.feature_flags import declared_flags

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "herdr_orchestrator"
TESTS = ROOT / "tests"
LIFECYCLE = ROOT / "docs" / "feature-flags.md"
EXAMPLE = ROOT / ".env.example"


def _python_text(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if path.name != "feature_flags.py"
    )


def main() -> int:
    production = _python_text(SOURCE)
    tests = _python_text(TESTS)
    lifecycle = LIFECYCLE.read_text(encoding="utf-8")
    example = EXAMPLE.read_text(encoding="utf-8")
    failures: list[str] = []
    declared_values = {flag.value for flag in declared_flags()}
    for flag, variable in declared_flags().items():
        if f"FeatureFlag.{flag.name}" not in production:
            failures.append(f"{flag.value}: no production consumer")
        if flag.value not in lifecycle:
            failures.append(f"{flag.value}: missing lifecycle row")
        if variable not in example:
            failures.append(f"{flag.value}: {variable} missing from .env.example")
        if flag.name not in tests and variable not in tests:
            failures.append(f"{flag.value}: no test reference")
    lifecycle_values = {
        line.split("`", 2)[1]
        for line in lifecycle.splitlines()
        if line.startswith("| `") and "`" in line[3:]
    }
    for removed in sorted(lifecycle_values - declared_values):
        failures.append(f"{removed}: lifecycle row has no declared flag")
    if failures:
        print("\n".join(failures))
        return 1
    print("feature flag policy: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
