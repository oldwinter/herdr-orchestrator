#!/usr/bin/env python3
"""Enforce repository size and technical-debt policies."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 512 * 1024
MAX_SOURCE_LINES = 1_500
MAX_TEST_LINES = 2_500
MAX_TEXT_LINES = 2_000
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".mjs", ".py", ".toml", ".yaml", ".yml"}
DEBT_MARKER = re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\b")
TRACKED_DEBT = re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\(#[1-9][0-9]* owner=[A-Za-z0-9_.-]+\):")
EXEMPT_SIZE_PATHS = {
    "src/herdr_orchestrator/dashboard/static/cytoscape.min.js",
}
EXEMPT_DEBT_PATHS = {"scripts/check_repository.py"}


def tracked_files() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return tuple(ROOT / value.decode() for value in result.stdout.split(b"\0") if value)


def line_limit(path: Path) -> int:
    relative = path.relative_to(ROOT)
    if path.suffix == ".py" and relative.parts[0] == "tests":
        return MAX_TEST_LINES
    if path.suffix == ".py":
        return MAX_SOURCE_LINES
    return MAX_TEXT_LINES


def main() -> int:
    failures: list[str] = []
    for path in tracked_files():
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        size = path.stat().st_size
        if size > MAX_BYTES and relative not in EXEMPT_SIZE_PATHS:
            failures.append(f"{relative}: {size} bytes exceeds {MAX_BYTES}")
        if path.suffix not in TEXT_SUFFIXES or relative in EXEMPT_SIZE_PATHS:
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.count("\n") + int(bool(text))
        maximum = line_limit(path)
        if lines > maximum:
            failures.append(f"{relative}: {lines} lines exceeds {maximum}")
        if relative in EXEMPT_DEBT_PATHS:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if DEBT_MARKER.search(line) and not TRACKED_DEBT.search(line):
                failures.append(
                    f"{relative}:{number}: debt marker needs issue and owner, "
                    "for example TODO(#123 owner=name):"
                )
    if failures:
        print("\n".join(failures))
        return 1
    print("repository policy: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
