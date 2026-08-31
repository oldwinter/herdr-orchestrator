#!/usr/bin/env python3
"""Validate local Markdown links and documented just recipes."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY_DOC_NAMES = ("README.md", "AGENTS.md", "CONTRIBUTING.md")
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
JUST_COMMAND = re.compile(r"`?just ([a-z][a-z0-9-]*)")
JUST_RECIPE = re.compile(r"^([a-z][a-z0-9-]*)(?: [^:]*)?:$", re.MULTILINE)


def documentation_failures(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    recipes = set(JUST_RECIPE.findall((root / "justfile").read_text(encoding="utf-8")))
    failures: list[str] = []
    for name in KEY_DOC_NAMES:
        document = root / name
        if document.is_symlink():
            failures.append(f"{name}: required document must be a regular file")
            continue
        if not document.is_file():
            failures.append(f"{name}: required document is missing")
            continue
        text = document.read_text(encoding="utf-8")
        for target in MARKDOWN_LINK.findall(text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path_text = target.split("#", maxsplit=1)[0]
            if path_text:
                target_path = (document.parent / path_text).resolve()
                if not target_path.is_relative_to(root):
                    failures.append(f"{document.name}: local link escapes repository {target}")
                elif not target_path.exists():
                    failures.append(f"{document.name}: missing local link {target}")
        for recipe in JUST_COMMAND.findall(text):
            if recipe not in recipes:
                failures.append(f"{document.name}: unknown just recipe {recipe}")
    return failures


def main() -> int:
    failures = documentation_failures()
    if failures:
        print("\n".join(failures))
        return 1
    print("documentation contracts: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
