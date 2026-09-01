"""Atomic publication helpers for quality command result files."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path


def publish_results(
    *,
    bundle_path: Path,
    default_path: Path,
    requested_path: Path | None,
    payload: object,
    write_json: Callable[[Path, object], None],
) -> None:
    paths = [default_path]
    if requested_path is not None and requested_path != default_path:
        paths.append(requested_path)
    new_paths = [path for path in paths if not path.exists()]
    try:
        for path in paths:
            write_json(path, payload)
    except (OSError, RuntimeError, shutil.Error):
        for path in new_paths:
            with suppress(OSError):
                path.unlink()
        with suppress(OSError, shutil.Error):
            shutil.rmtree(bundle_path)
        raise
