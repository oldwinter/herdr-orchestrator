#!/usr/bin/env bash
# Read-only: is this isolated verification instance worth driving?
# Exit 0 only when OUR pid owns the printed URL and queue snapshot is coherent.

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

RUN_ID="$(resolve_run_id)"
RUN_JSON="$(require_run_json "$RUN_ID")"
SCRATCH="$(run_dir "$RUN_ID")"
EVIDENCE="$(evidence_dir "$RUN_ID")"
mkdir -p "$EVIDENCE"

PID="$(json_get "$RUN_JSON" pid)"
URL="$(json_get "$RUN_JSON" url)"
STATE_DB="$(json_get "$RUN_JSON" state_db)"
WORKFLOW="$(json_get "$RUN_JSON" workflow)"
WORKFLOW_NAME="$(json_get "$RUN_JSON" workflow_name)"
ROOT="$(repo_root)"

if [[ "$STATE_DB" == "$ROOT/.orchestrator/state.db" ]]; then
  echo "herdr-verify doctor: refused default state_db" >&2
  exit 2
fi
if [[ "$WORKFLOW" == "$ROOT/workflows/multi-harness.toml" ]]; then
  echo "herdr-verify doctor: refused default workflow" >&2
  exit 2
fi
if [[ "$WORKFLOW_NAME" != "$VERIFY_WORKFLOW_NAME" ]]; then
  echo "herdr-verify doctor: unexpected workflow name $WORKFLOW_NAME" >&2
  exit 2
fi

if ! kill -0 "$PID" 2>/dev/null; then
  echo "herdr-verify doctor: pid $PID is not running" >&2
  exit 1
fi

CMDLINE="$(tr '\0' ' ' <"/proc/$PID/cmdline" || true)"
case "$CMDLINE" in
  *herdr_orchestrator*dashboard*) ;;
  *)
    echo "herdr-verify doctor: pid $PID cmdline is not our dashboard: $CMDLINE" >&2
    exit 2
    ;;
esac

if [[ ! -f "$STATE_DB" ]]; then
  echo "herdr-verify doctor: state_db missing: $STATE_DB" >&2
  exit 1
fi

if ! host_header_ok_curl "$URL/api/health" -o "$EVIDENCE/health.json"; then
  echo "herdr-verify doctor: /api/health failed for $URL" >&2
  exit 1
fi

python3 - "$EVIDENCE/health.json" <<'PY'
import json, sys
health = json.load(open(sys.argv[1], encoding="utf-8"))
if health.get("ok") is not True:
    raise SystemExit("health_not_ok")
PY

host_header_ok_curl "$URL/api/snapshot" -o "$EVIDENCE/snapshot.json"

ho status --workflow "$WORKFLOW" >"$EVIDENCE/status.json"

python3 - "$RUN_JSON" "$EVIDENCE/snapshot.json" "$EVIDENCE/status.json" "$EVIDENCE/doctor.json" \
  "$SEED_TITLE_DROID" "$SEED_TITLE_CODEX" "$VERIFY_WORKFLOW_NAME" <<'PY'
import json, sys

run, snap_path, status_path, dest, title_droid, title_codex, workflow_name = sys.argv[1:]
snap_wrap = json.load(open(snap_path, encoding="utf-8"))
status = json.load(open(status_path, encoding="utf-8"))
snapshot = snap_wrap.get("snapshot") or {}
summary = snapshot.get("summary") or {}
jobs = snapshot.get("jobs") or []
titles = {job.get("title") for job in jobs}
pending = [job for job in jobs if job.get("state") == "pending"]
errors = []
if snapshot.get("workflow") != workflow_name:
    errors.append(f"snapshot.workflow={snapshot.get('workflow')}")
if status.get("workflow") != workflow_name:
    errors.append(f"status.workflow={status.get('workflow')}")
if title_droid not in titles or title_codex not in titles:
    errors.append(f"missing seed titles: {sorted(titles)}")
if summary.get("pending", 0) < 2:
    errors.append(f"summary.pending={summary.get('pending')}")
if len(pending) < 2:
    errors.append(f"pending jobs={len(pending)}")
if (snapshot.get("source_health") or {}).get("queue") != "ok":
    errors.append(f"queue health={(snapshot.get('source_health') or {}).get('queue')}")
report = {
    "ok": not errors,
    "run_id": json.load(open(run, encoding="utf-8"))["run_id"],
    "url": json.load(open(run, encoding="utf-8"))["url"],
    "pid": json.load(open(run, encoding="utf-8"))["pid"],
    "workflow": workflow_name,
    "job_count": len(jobs),
    "pending": summary.get("pending"),
    "herdr": (snapshot.get("source_health") or {}).get("herdr"),
    "herdr_error": (snapshot.get("source_health") or {}).get("herdr_error"),
    "errors": errors,
}
json.dump(report, open(dest, "w", encoding="utf-8"), indent=2, sort_keys=True)
print(json.dumps(report, indent=2, sort_keys=True))
if errors:
    raise SystemExit(1)
PY
