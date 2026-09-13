#!/usr/bin/env bash
# Drive the Work in flight board the way an operator reads it:
# isolated seed already happened at launch; prove the live page projects those jobs.

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

HELPERS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HELPERS/doctor.sh"

RUN_ID="$(resolve_run_id)"
RUN_JSON="$(require_run_json "$RUN_ID")"
EVIDENCE="$(evidence_dir "$RUN_ID")"
URL="$(json_get "$RUN_JSON" url)"

"$HELPERS/capture-dashboard.sh" >/dev/null

python3 - "$EVIDENCE" "$SEED_TITLE_DROID" "$SEED_TITLE_CODEX" "$VERIFY_WORKFLOW_NAME" "$URL" <<'PY'
import json, pathlib, sys

evidence = pathlib.Path(sys.argv[1])
title_droid, title_codex, workflow_name, url = sys.argv[2:]
dom = (evidence / "page.html").read_text(encoding="utf-8", errors="replace")
snapshot = json.loads((evidence / "snapshot.json").read_text(encoding="utf-8"))["snapshot"]
summary = snapshot["summary"]
jobs = snapshot["jobs"]
titles = [job["title"] for job in jobs]
errors = []

def need(cond, message):
    if not cond:
        errors.append(message)

need(snapshot.get("workflow") == workflow_name, f"workflow {snapshot.get('workflow')}")
need(title_droid in titles, f"missing {title_droid}")
need(title_codex in titles, f"missing {title_codex}")
need(summary.get("pending", 0) >= 2, f"pending {summary.get('pending')}")
need(title_droid in dom, "dump-dom missing droid title")
need(title_codex in dom, "dump-dom missing codex title")
need('data-column-key="queued"' in dom, "dump-dom missing queued column")
need("data-job-id=" in dom, "dump-dom missing job cards")
need("Herdr Operations" in dom, "dump-dom missing page title")
need(workflow_name in dom, "dump-dom missing workflow name")
need('id="metric-pending"' in dom, "dump-dom missing pending metric")
screenshot = evidence / "board.png"
need(screenshot.is_file() and screenshot.stat().st_size > 1000, "screenshot missing or tiny")

report = {
    "feature": "dashboard-queue-board",
    "ok": not errors,
    "url": url,
    "titles": titles,
    "pending": summary.get("pending"),
    "connection_in_dom": "Live" in dom or "Connected" in dom,
    "herdr_warning_expected_without_runtime": "Herdr observation unavailable" in dom,
    "errors": errors,
    "evidence": {
        "snapshot": str(evidence / "snapshot.json"),
        "status": str(evidence / "status.json"),
        "dom": str(evidence / "page.html"),
        "screenshot": str(screenshot),
        "doctor": str(evidence / "doctor.json"),
    },
}
(evidence / "drive-dashboard-board.json").write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(json.dumps(report, indent=2, sort_keys=True))
if errors:
    raise SystemExit(1)
PY
