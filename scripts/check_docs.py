#!/usr/bin/env python3
"""Validate local Markdown links and documented just recipes."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY_DOC_NAMES = ("README.md", "AGENTS.md", "CONTRIBUTING.md")
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
JUST_COMMAND = re.compile(r"`?just ([a-z][a-z0-9-]*)")
JUST_RECIPE = re.compile(r"^([a-z][a-z0-9-]*)(?: [^:]*)?:$", re.MULTILINE)
SHELL_BLOCK = re.compile(r"```(?:bash|sh|shell|console)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
INLINE_COMMAND = re.compile(r"^(?:just|python(?:3)?|uv|npx|npm|herdr|\$)\b")
PATH_OPTIONS = {
    "--config",
    "--cwd",
    "--goal-file",
    "--output-file",
    "--path",
    "--project",
    "--prompt-file",
    "--receipt-file",
    "--response-file",
    "--workflow",
}
PATH_COMMANDS = {
    "cd",
    "cat",
    "cp",
    "find",
    "head",
    "less",
    "ls",
    "mkdir",
    "mv",
    "pushd",
    "readlink",
    "realpath",
    "rm",
    "sed",
    "tail",
    "touch",
}


def documentation_failures(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    recipes = set(JUST_RECIPE.findall((root / "justfile").read_text(encoding="utf-8")))
    repository_roots = {
        path.name for path in root.iterdir() if path.name not in {".git", ".orchestrator"}
    }
    failures: list[str] = []
    names = [*KEY_DOC_NAMES]
    docs_root = root / "docs"
    if docs_root.is_dir():
        names.extend(
            path.relative_to(root).as_posix()
            for path in sorted(docs_root.rglob("*.md"))
            if path.name != "installation.md"
        )
    for name in dict.fromkeys(names):
        document = root / name
        if document.is_symlink():
            prefix = "required" if name in KEY_DOC_NAMES else "reference"
            failures.append(f"{name}: {prefix} document must be a regular file")
            continue
        if not document.is_file():
            if name in KEY_DOC_NAMES:
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
        failures.extend(_command_path_failures(document, text, root, repository_roots))
    return failures


def _command_path_failures(
    document: Path,
    text: str,
    root: Path,
    repository_roots: set[str],
) -> list[str]:
    failures: list[str] = []
    seen: set[str] = set()
    snippets = SHELL_BLOCK.findall(text)
    snippets.extend(span for span in CODE_SPAN.findall(text) if INLINE_COMMAND.match(span.strip()))
    for snippet in snippets:
        normalized = snippet.replace("\\\n", " ")
        for line in normalized.splitlines():
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                tokens = line.split()
            expect_path = False
            for token in tokens:
                candidate = _command_path_token(token, expect_path=expect_path)
                expect_path = _path_option(token) or token in PATH_COMMANDS
                if candidate is None or candidate in seen:
                    continue
                seen.add(candidate)
                if candidate.startswith(".orchestrator/"):
                    continue
                if candidate.startswith("../"):
                    path = (root / candidate).resolve()
                else:
                    first = candidate.split("/", maxsplit=1)[0]
                    if first not in repository_roots:
                        continue
                    path = (root / candidate).resolve()
                if not path.is_relative_to(root):
                    failures.append(f"{document.name}: command path escapes repository {candidate}")
                elif not path.exists():
                    failures.append(f"{document.name}: missing command path {candidate}")
    return failures


def _command_path_token(token: str, *, expect_path: bool) -> str | None:
    path_hint = expect_path
    if "=" in token:
        option, value = token.split("=", maxsplit=1)
        if not option.startswith("-") or _path_option(option):
            token = value
            path_hint = path_hint or _path_option(option)
    while token.startswith("./"):
        token = token[2:]
    token = token.split("#", maxsplit=1)[0].rstrip(".,;:)]}")
    if (
        not token
        or token in {".", "..", "\\"}
        or token.startswith(("-", "$", "<", ">", "~", "/"))
        or "://" in token
        or any(char in token for char in "*?[]")
    ):
        return None
    return token if path_hint or "/" in token else None


def _path_option(token: str) -> bool:
    option = token.split("=", maxsplit=1)[0]
    return option.endswith("PATH") or (
        option.startswith("-")
        and (option in PATH_OPTIONS or option.endswith(("-file", "-path", "-root")))
    )


def main() -> int:
    failures = documentation_failures()
    if failures:
        print("\n".join(failures))
        return 1
    print("documentation contracts: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
