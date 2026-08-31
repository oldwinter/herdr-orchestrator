#!/usr/bin/env python3
"""Reject undeclared, unused, or undocumented feature flags."""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path
from typing import cast

from herdr_orchestrator.feature_flags import FeatureFlag, declared_flags

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "herdr_orchestrator"
TESTS = ROOT / "tests"
LIFECYCLE = ROOT / "docs" / "feature-flags.md"
OBSERVABILITY = ROOT / "docs" / "observability.md"
EXAMPLE = ROOT / ".env.example"
_ENV_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(?P<value>[^\r\n#]*)",
    re.MULTILINE,
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _python_references(
    root: Path,
    *,
    exclude_name: str | None = None,
) -> tuple[set[str], list[str]]:
    references: set[str] = set()
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if exclude_name is not None and path.name == exclude_name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError) as exc:
            failures.append(f"{path}: unable to read Python source: {type(exc).__name__}")
            continue
        except (SyntaxError, ValueError) as exc:
            failures.append(f"{path}:{getattr(exc, 'lineno', 0) or 0}: Python parse failed")
            continue
        aliases: set[str] = set()
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                (node.level == 0 and node.module == "herdr_orchestrator.feature_flags")
                or (node.level > 0 and node.module == "feature_flags")
            ):
                for alias in node.names:
                    if alias.name == "FeatureFlag":
                        aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "herdr_orchestrator.feature_flags":
                        module_aliases.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            chain: list[str] = []
            current: ast.expr = node
            while isinstance(current, ast.Attribute):
                chain.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                chain.append(current.id)
            chain.reverse()
            if (
                len(chain) >= 2
                and chain[-2] in aliases
                or (
                    len(chain) >= 3
                    and chain[-2] == "FeatureFlag"
                    and ".".join(chain[:-2]) in module_aliases
                )
            ):
                references.add(chain[-1])
    return {name for name in references if name.isupper()}, failures


def _lifecycle_rows(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    rows: list[tuple[str, tuple[str, ...]]] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = tuple(cell.strip() for cell in line.strip().split("|")[1:-1])
        if not cells or len(cells[0]) < 2 or not cells[0].startswith("`"):
            continue
        if not cells[0].endswith("`"):
            continue
        rows.append((cells[0][1:-1], cells))
    return tuple(rows)


def _markdown_table_values(text: str) -> set[str]:
    values: set[str] = set()
    for line in _HTML_COMMENT.sub("", text).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        for cell in line.strip().split("|")[1:-1]:
            cell = cell.strip()
            if len(cell) >= 2 and cell.startswith("`") and cell.endswith("`"):
                values.add(cell[1:-1])
    return values


def _environment_assignments(text: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for match in _ENV_ASSIGNMENT.finditer(text):
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        assignments[match.group("name")] = value
    return assignments


def _read(path: Path, failures: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(f"{path}: unable to read: {type(exc).__name__}")
        return ""


def policy_failures(root: Path | None = None) -> list[str]:
    if root is None:
        source, tests = SOURCE, TESTS
        lifecycle_path, observability_path, example_path = LIFECYCLE, OBSERVABILITY, EXAMPLE
    else:
        source = root / "src" / "herdr_orchestrator"
        tests = root / "tests"
        lifecycle_path = root / "docs" / "feature-flags.md"
        observability_path = root / "docs" / "observability.md"
        example_path = root / ".env.example"
    failures: list[str] = []

    for path in (source, tests):
        if not path.is_dir():
            failures.append(f"{path}: missing directory")
    for path in (lifecycle_path, observability_path, example_path):
        if not path.is_file():
            failures.append(f"{path}: missing file")

    try:
        declarations = cast(dict[object, object], dict(declared_flags()))
    except (TypeError, ValueError) as exc:
        failures.append(f"feature_flags.py: declaration load failed: {type(exc).__name__}")
        return failures

    expected_flags = set(FeatureFlag)
    declared_names: set[str] = set()
    variables: dict[FeatureFlag, str] = {}
    for flag, variable in declarations.items():
        if not isinstance(flag, FeatureFlag):
            failures.append("feature_flags.py: mapping contains an unknown flag")
            continue
        declared_names.add(flag.name)
        if not isinstance(variable, str) or not variable:
            failures.append(f"{flag.value}: environment mapping is invalid")
            continue
        variables[flag] = variable
    for missing in sorted(expected_flags - set(variables), key=lambda item: item.value):
        failures.append(f"{missing.value}: missing environment mapping")
    for extra in sorted(set(declarations) - expected_flags, key=str):
        if isinstance(extra, FeatureFlag):
            continue
        failures.append(f"{extra}: mapping has no declared flag")
    duplicate_variables = [
        variable for variable, count in Counter(variables.values()).items() if count > 1
    ]
    for variable in sorted(duplicate_variables):
        failures.append(f"{variable}: environment mapping is duplicated")

    production, production_failures = _python_references(
        source,
        exclude_name="feature_flags.py",
    )
    tests_found, test_failures = _python_references(tests)
    failures.extend(production_failures)
    failures.extend(test_failures)
    for unknown in sorted(production - declared_names):
        failures.append(f"{unknown}: production reference has no declared flag")
    for unknown in sorted(tests_found - declared_names):
        failures.append(f"{unknown}: test reference has no declared flag")

    lifecycle = _read(lifecycle_path, failures) if lifecycle_path.is_file() else ""
    observability = _read(observability_path, failures) if observability_path.is_file() else ""
    example = _read(example_path, failures) if example_path.is_file() else ""
    lifecycle_rows = _lifecycle_rows(lifecycle)
    lifecycle_values = tuple(value for value, _ in lifecycle_rows)
    lifecycle_counts = Counter(lifecycle_values)
    for value, count in sorted(lifecycle_counts.items()):
        if count > 1:
            failures.append(f"{value}: duplicate lifecycle rows")
    for value, cells in lifecycle_rows:
        if len(cells) < 6 or any(not cells[index] for index in (1, 2, 3, 4, 5)):
            failures.append(f"{value}: lifecycle row is incomplete")
    declared_values = {flag.value for flag in variables}
    environment_assignments = _environment_assignments(example)
    declared_variables = set(variables.values())
    for unknown in sorted(
        name
        for name in environment_assignments
        if name.startswith("HERDR_FEATURE_") and name not in declared_variables
    ):
        failures.append(f"{unknown}: .env.example entry has no declared flag")
    observability_values = _markdown_table_values(observability)
    unknown_observability = {
        value
        for value in observability_values
        if value.startswith("HERDR_FEATURE_") and value not in declared_variables
    }
    for unknown in sorted(unknown_observability):
        failures.append(f"{unknown}: observability entry has no declared flag")
    for flag, variable in variables.items():
        if flag.name not in production:
            failures.append(f"{flag.value}: no production consumer")
        if lifecycle_values.count(flag.value) == 0:
            failures.append(f"{flag.value}: missing lifecycle row")
        if variable not in environment_assignments:
            failures.append(f"{flag.value}: {variable} missing from .env.example")
        elif environment_assignments[variable].casefold() != "false":
            failures.append(f"{flag.value}: {variable} must default to false")
        if variable not in observability_values:
            failures.append(f"{flag.value}: {variable} missing from docs/observability.md")
        if flag.name not in tests_found:
            failures.append(f"{flag.value}: no test reference")
    for removed in sorted(set(lifecycle_values) - declared_values):
        failures.append(f"{removed}: lifecycle row has no declared flag")
    return failures


def main() -> int:
    failures = policy_failures()
    if failures:
        print("\n".join(failures))
        return 1
    print("feature flag policy: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
