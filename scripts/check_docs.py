#!/usr/bin/env python3
"""Validate local Markdown links and documented just recipes."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY_DOCS = (ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "CONTRIBUTING.md")
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
JUST_COMMAND = re.compile(r"`?just ([a-z][a-z0-9-]*)")
JUST_RECIPE = re.compile(r"^([a-z][a-z0-9-]*)(?: [^:]*)?:$", re.MULTILINE)


def main() -> int:
    recipes = set(JUST_RECIPE.findall((ROOT / "justfile").read_text(encoding="utf-8")))
    failures: list[str] = []
    for document in KEY_DOCS:
        if not document.exists():
            continue
        text = document.read_text(encoding="utf-8")
        for target in MARKDOWN_LINK.findall(text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path_text = target.split("#", maxsplit=1)[0]
            if path_text and not (document.parent / path_text).resolve().exists():
                failures.append(f"{document.name}: missing local link {target}")
        for recipe in JUST_COMMAND.findall(text):
            if recipe not in recipes:
                failures.append(f"{document.name}: unknown just recipe {recipe}")
    if failures:
        print("\n".join(failures))
        return 1
    print("documentation contracts: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
