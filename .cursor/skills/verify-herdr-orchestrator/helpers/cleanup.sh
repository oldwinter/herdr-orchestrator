#!/usr/bin/env bash
# Tear down the isolated Dashboard this run started. Keep evidence.

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

RUN_ID="$(resolve_run_id)"
RUN_JSON="$(require_run_json "$RUN_ID")"
SCRATCH="$(run_dir "$RUN_ID")"
EVIDENCE="$(evidence_dir "$RUN_ID")"
PID="$(json_get "$RUN_JSON" pid)"
ROOT="$(repo_root)"
STATE_DB="$(json_get "$RUN_JSON" state_db)"

if [[ "$STATE_DB" == "$ROOT/.orchestrator/state.db" ]]; then
  echo "herdr-verify cleanup: refusing to touch default state_db" >&2
  exit 2
fi
case "$SCRATCH" in
  "$ROOT/.orchestrator/verify-scratch/"*) ;;
  *)
    echo "herdr-verify cleanup: scratch path is not isolated: $SCRATCH" >&2
    exit 2
    ;;
esac

if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  CMDLINE="$(tr '\0' ' ' <"/proc/$PID/cmdline" || true)"
  case "$CMDLINE" in
    *herdr_orchestrator*dashboard*)
      kill -TERM "$PID" 2>/dev/null || true
      for _ in $(seq 1 20); do
        if ! kill -0 "$PID" 2>/dev/null; then
          break
        fi
        sleep 0.2
      done
      if kill -0 "$PID" 2>/dev/null; then
        kill -KILL "$PID" 2>/dev/null || true
      fi
      ;;
    *)
      echo "herdr-verify cleanup: pid $PID is not our dashboard; not killing" >&2
      exit 2
      ;;
  esac
fi

POINTER="$(current_pointer)"
if [[ -f "$POINTER" && "$(cat "$POINTER")" == "$RUN_ID" ]]; then
  rm -f "$POINTER"
fi

rm -rf "$SCRATCH"

if [[ ! -d "$EVIDENCE" ]]; then
  echo "herdr-verify cleanup: evidence directory missing after teardown: $EVIDENCE" >&2
  exit 1
fi

python3 -c '
import json, sys
print(json.dumps({
    "cleaned_run": sys.argv[1],
    "scratch_removed": True,
    "evidence_dir": sys.argv[2],
}, indent=2, sort_keys=True))
' "$RUN_ID" "$EVIDENCE"
