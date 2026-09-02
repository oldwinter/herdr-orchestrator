#!/usr/bin/env python3
"""Capture cProfile evidence while preserving the pytest exit code."""

from __future__ import annotations

import argparse
import cProfile
from pathlib import Path

import pytest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profiler = cProfile.Profile()
    status = profiler.runcall(pytest.main, ["tests/test_protocol.py", "-q"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    profiler.dump_stats(args.output)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
