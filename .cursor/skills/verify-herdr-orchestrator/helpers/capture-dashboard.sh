#!/usr/bin/env bash
# Render the live Dashboard in headless Chrome and keep PNG + dump-dom.

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

RUN_ID="$(resolve_run_id)"
RUN_JSON="$(require_run_json "$RUN_ID")"
EVIDENCE="$(evidence_dir "$RUN_ID")"
URL="$(json_get "$RUN_JSON" url)"
mkdir -p "$EVIDENCE"

CHROME=""
for candidate in google-chrome google-chrome-stable chromium chromium-browser google-chrome-beta; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CHROME="$candidate"
    break
  fi
done

if [[ -z "$CHROME" ]]; then
  echo "herdr-verify capture: no Chrome/Chromium on PATH" >&2
  exit 3
fi

PROFILE="$EVIDENCE/chrome-profile"
mkdir -p "$PROFILE"
SCREENSHOT="$EVIDENCE/board.png"
DOM="$EVIDENCE/page.html"

"$CHROME" \
  --headless=new \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --hide-scrollbars \
  --window-size=1440,1400 \
  --user-data-dir="$PROFILE" \
  --virtual-time-budget=8000 \
  --screenshot="$SCREENSHOT" \
  --dump-dom \
  "$URL" >"$DOM" 2>"$EVIDENCE/chrome.err"

if [[ ! -s "$SCREENSHOT" ]]; then
  echo "herdr-verify capture: empty screenshot" >&2
  exit 1
fi
if [[ ! -s "$DOM" ]]; then
  echo "herdr-verify capture: empty dump-dom" >&2
  exit 1
fi

python3 -c '
import json, sys
print(json.dumps({
    "url": sys.argv[1],
    "screenshot": sys.argv[2],
    "dom": sys.argv[3],
    "chrome": sys.argv[4],
}, indent=2, sort_keys=True))
' "$URL" "$SCREENSHOT" "$DOM" "$CHROME"
