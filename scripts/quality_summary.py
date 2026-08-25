#!/usr/bin/env python3
"""Summarize machine-readable quality results for humans and PR automation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUALITY_ROOT = ROOT / ".orchestrator" / "quality"


def load(name: str) -> dict[str, object]:
    path = QUALITY_ROOT / name
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    coverage = load("coverage.json")
    stability = load("stability.json")
    build = load("build.json")
    bandit = load("bandit.json")
    audit = load("pip-audit.json")
    totals = coverage.get("totals", {})
    percent = totals.get("percent_covered", "unknown") if isinstance(totals, dict) else "unknown"
    bandit_results = bandit.get("results", [])
    bandit_issues = len(bandit_results) if isinstance(bandit_results, list) else 0
    audit_issues = 0
    dependencies = audit.get("dependencies", [])
    if isinstance(dependencies, list):
        audit_issues = sum(
            len(item.get("vulns", [])) for item in dependencies if isinstance(item, dict)
        )
    lines = [
        "## Automated quality review",
        "",
        f"- Coverage: **{percent}%** with an enforced 80% branch-aware threshold",
        f"- Stability: **{stability.get('runs', 0)}** repeated runs, "
        f"**{len(stability.get('unstable', []))}** unstable tests",
        f"- Build: **{build.get('duration_seconds', 'unknown')}s**, "
        f"**{build.get('package_size_bytes', 'unknown')} bytes** packed",
        f"- Security: **{bandit_issues}** medium/high Bandit findings, "
        f"**{audit_issues}** dependency vulnerabilities",
        "",
        "Generated from pinned local tools. Review failures before merge.",
        "",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
