#!/usr/bin/env bash
# Create an isolated workflow + state_db, seed two pending jobs, start Dashboard.
# Never touches workflows/multi-harness.toml or .orchestrator/state.db.

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

ROOT="$(repo_root)"
SKILL="$(skill_dir)"
RUN_ID="${HERDR_VERIFY_RUN:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
SCRATCH="$(run_dir "$RUN_ID")"
EVIDENCE="$(evidence_dir "$RUN_ID")"
TEMPLATE="$SKILL/workflow.template.toml"
WORKFLOW="$SCRATCH/workflow.toml"
STATE_DB="$SCRATCH/state.db"

if [[ -e "$SCRATCH" ]]; then
  echo "herdr-verify: scratch already exists: $SCRATCH" >&2
  exit 2
fi

mkdir -p "$SCRATCH/worktrees" "$SCRATCH/deliveries" "$EVIDENCE"

python3 - "$TEMPLATE" "$WORKFLOW" "$ROOT" "$STATE_DB" "$SCRATCH" <<'PY'
import pathlib, sys
template, dest, workspace, state_db, scratch = map(pathlib.Path, sys.argv[1:])
prompts = workspace / "workflows" / "prompts"
text = template.read_text(encoding="utf-8")
replacements = {
    "__WORKSPACE__": str(workspace),
    "__STATE_DB__": str(state_db),
    "__PROFILES_DIR__": str(workspace / "profiles" / "harnesses"),
    "__WORKTREE_ROOT__": str(scratch / "worktrees"),
    "__PLANNER_PROMPT__": str(prompts / "planner.md"),
    "__PLANNER_OUTPUT__": str(scratch / "plans.json"),
    "__SEED_PROMPT_DROID__": str(prompts / "droid-inventory.md"),
    "__SEED_PROMPT_CODEX__": str(prompts / "codex-architecture.md"),
}
for key, value in replacements.items():
    text = text.replace(key, value)
dest.write_text(text, encoding="utf-8")
PY

echo "herdr-verify: seeding $WORKFLOW" >&2
SEED_JSON="$(ho seed --workflow "$WORKFLOW")"
printf '%s\n' "$SEED_JSON" | tee "$EVIDENCE/seed.json"

ADDED="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["added"])' <<<"$SEED_JSON")"
if [[ "$ADDED" != "2" ]]; then
  echo "herdr-verify: expected seed added=2, got $ADDED" >&2
  exit 1
fi

if [[ "$STATE_DB" == "$ROOT/.orchestrator/state.db" ]]; then
  echo "herdr-verify: refused to use the default state_db" >&2
  exit 2
fi

LOG="$SCRATCH/dashboard.log"
# exec so $! is the Python dashboard, not this helper's bash wrapper.
(
  cd "$ROOT"
  export PYTHONPATH=src
  if command -v uv >/dev/null 2>&1; then
    exec uv run python -m herdr_orchestrator dashboard \
      --workflow "$WORKFLOW" --host 127.0.0.1 --port 0 --poll-seconds 1
  else
    exec python3 -m herdr_orchestrator dashboard \
      --workflow "$WORKFLOW" --host 127.0.0.1 --port 0 --poll-seconds 1
  fi
) >"$LOG" 2>"$SCRATCH/dashboard.err" &
DASH_PID=$!
printf '%s\n' "$DASH_PID" >"$SCRATCH/dashboard.pid"

cleanup_failed_launch() {
  if kill -0 "$DASH_PID" 2>/dev/null; then
    kill -TERM "$DASH_PID" 2>/dev/null || true
    wait "$DASH_PID" 2>/dev/null || true
  fi
}

trap cleanup_failed_launch EXIT

URL=""
for _ in $(seq 1 40); do
  if [[ -s "$LOG" ]]; then
    URL="$(python3 - "$LOG" <<'PY'
import json, sys
path = sys.argv[1]
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        continue
    if payload.get("status") == "dashboard_started" and payload.get("url"):
        print(payload["url"])
        break
PY
)"
  fi
  if [[ -n "$URL" ]]; then
    break
  fi
  if ! kill -0 "$DASH_PID" 2>/dev/null; then
    echo "herdr-verify: dashboard exited during startup" >&2
    cat "$LOG" "$SCRATCH/dashboard.err" >&2 || true
    exit 1
  fi
  sleep 0.25
done

if [[ -z "$URL" ]]; then
  echo "herdr-verify: dashboard did not print dashboard_started" >&2
  cat "$LOG" "$SCRATCH/dashboard.err" >&2 || true
  exit 1
fi

HEALTH_OK=""
for _ in $(seq 1 40); do
  if host_header_ok_curl "$URL/api/health" -o "$EVIDENCE/health.json"; then
    HEALTH_OK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("ok"))' "$EVIDENCE/health.json")"
    if [[ "$HEALTH_OK" == "True" ]]; then
      break
    fi
  fi
  sleep 0.25
done

if [[ "$HEALTH_OK" != "True" ]]; then
  echo "herdr-verify: /api/health never became ok" >&2
  exit 1
fi

python3 - "$SCRATCH/run.json" "$RUN_ID" "$WORKFLOW" "$STATE_DB" "$URL" "$DASH_PID" "$EVIDENCE" <<'PY'
import json, sys
dest, run_id, workflow, state_db, url, pid, evidence = sys.argv[1:]
payload = {
    "run_id": run_id,
    "workflow_name": "verify-orchestrator",
    "workflow": workflow,
    "state_db": state_db,
    "url": url,
    "pid": int(pid),
    "evidence_dir": evidence,
}
json.dump(payload, open(dest, "w", encoding="utf-8"), indent=2, sort_keys=True)
print(json.dumps(payload, indent=2, sort_keys=True))
PY

cp "$SCRATCH/run.json" "$EVIDENCE/launch.json"
printf '%s\n' "$RUN_ID" >"$(current_pointer)"
echo "$RUN_ID" >"$EVIDENCE/run-id.txt"

trap - EXIT
echo "herdr-verify: launched $URL (run $RUN_ID, pid $DASH_PID)" >&2
