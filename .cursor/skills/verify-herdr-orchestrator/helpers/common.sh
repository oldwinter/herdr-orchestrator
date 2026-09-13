#!/usr/bin/env bash
# Shared paths and the isolated-workflow CLI wrapper.
# Source this file; do not execute it.

set -euo pipefail

_COMMON_SH="${BASH_SOURCE[0]}"

skill_dir() {
  cd "$(dirname "$_COMMON_SH")/.." && pwd
}

repo_root() {
  local skill
  skill="$(skill_dir)"
  cd "$skill/../../.." && pwd
}

VERIFY_WORKFLOW_NAME="verify-orchestrator"
SEED_TITLE_DROID="Verify droid inventory"
SEED_TITLE_CODEX="Verify codex architecture"

scratch_root() {
  printf '%s/.orchestrator/verify-scratch\n' "$(repo_root)"
}

evidence_root() {
  printf '%s/.orchestrator/verify-evidence\n' "$(repo_root)"
}

current_pointer() {
  printf '%s/current\n' "$(scratch_root)"
}

run_dir() {
  local run_id="$1"
  printf '%s/%s\n' "$(scratch_root)" "$run_id"
}

evidence_dir() {
  local run_id="$1"
  printf '%s/%s\n' "$(evidence_root)" "$run_id"
}

resolve_run_id() {
  if [[ -n "${HERDR_VERIFY_RUN:-}" ]]; then
    printf '%s\n' "$HERDR_VERIFY_RUN"
    return 0
  fi
  local pointer
  pointer="$(current_pointer)"
  if [[ -f "$pointer" ]]; then
    cat "$pointer"
    return 0
  fi
  echo "herdr-verify: no isolated run. Launch first with helpers/launch.sh" >&2
  echo "Do not attach to just dashboard / .orchestrator/state.db." >&2
  return 2
}

require_run_json() {
  local run_id="$1"
  local path
  path="$(run_dir "$run_id")/run.json"
  if [[ ! -f "$path" ]]; then
    echo "herdr-verify: missing $path" >&2
    return 2
  fi
  printf '%s\n' "$path"
}

ho() {
  local root
  root="$(repo_root)"
  if command -v uv >/dev/null 2>&1; then
    (cd "$root" && PYTHONPATH=src uv run python -m herdr_orchestrator "$@")
  else
    (cd "$root" && PYTHONPATH=src python3 -m herdr_orchestrator "$@")
  fi
}

json_get() {
  local file="$1"
  local key="$2"
  python3 -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
key = sys.argv[2]
value = data
for part in key.split("."):
    if isinstance(value, dict) and part in value:
        value = value[part]
    else:
        sys.exit(3)
if isinstance(value, (dict, list)):
    json.dump(value, sys.stdout)
else:
    print(value)
' "$file" "$key"
}

host_header_ok_curl() {
  local url="$1"
  shift
  curl -fsS --max-time 5 -H "Accept: application/json" "$url" "$@"
}
