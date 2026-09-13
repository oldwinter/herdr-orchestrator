#!/usr/bin/env bash
# Render the live Dashboard via CDP. Do not use chrome --dump-dom: EventSource never idles.

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

HELPERS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID="$(resolve_run_id)"
RUN_JSON="$(require_run_json "$RUN_ID")"
EVIDENCE="$(evidence_dir "$RUN_ID")"
URL="$(json_get "$RUN_JSON" url)"
mkdir -p "$EVIDENCE"

CHROME=""
for candidate in \
  /usr/bin/google-chrome-stable \
  /opt/google/chrome/chrome \
  /opt/google/chrome/google-chrome \
  /usr/bin/chromium \
  /usr/bin/chromium-browser
do
  if [[ -x "$candidate" ]]; then
    CHROME="$candidate"
    break
  fi
done

if [[ -z "$CHROME" ]]; then
  echo "herdr-verify capture: no Chrome/Chromium binary (skip /usr/local/bin wrappers)" >&2
  exit 3
fi

PROFILE="$(run_dir "$RUN_ID")/chrome-profile"
mkdir -p "$PROFILE"
SCREENSHOT="$EVIDENCE/board.png"
DOM="$EVIDENCE/page.html"

timeout --signal=TERM --kill-after=5s 30s \
  node "$HELPERS/capture-dashboard.mjs" \
    "$URL" "$SCREENSHOT" "$DOM" "$CHROME" "$PROFILE" \
  | tee "$EVIDENCE/capture.json"

if [[ ! -s "$SCREENSHOT" ]]; then
  echo "herdr-verify capture: empty screenshot" >&2
  exit 1
fi
if [[ ! -s "$DOM" ]]; then
  echo "herdr-verify capture: empty dump-dom" >&2
  exit 1
fi
