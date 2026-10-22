#!/usr/bin/env bash

set -Eeuo pipefail

# One-shot bootstrap for the initial COS migration. This script intentionally
# runs outside the Backend/Scheduler path: local Chrome is the source egress,
# while the existing sync script owns COS and Mapping writes.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE_FILE="${1:-}"
if [[ -z "$SOURCE_FILE" ]]; then
  echo "usage: $0 SOURCE_JSONL [atlas_browser_sync.py options...]" >&2
  exit 2
fi
shift

PORT="${QG_BROWSER_PORT:-8790}"
TOKEN="${QG_BROWSER_BRIDGE_TOKEN:-$(openssl rand -hex 32)}"
PROBE_URL="${QG_BROWSER_PROBE_URL:-}"
CHROME_PATH="${QG_BROWSER_CHROME_PATH:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PROFILE_DIR="${QG_BROWSER_PROFILE_DIR:-${TMPDIR:-/tmp}/qg-atlas-bootstrap-profile}"
LOG_FILE="${QG_BROWSER_LOG_FILE:-${TMPDIR:-/tmp}/qg-atlas-browser-worker.log}"

if [[ ! -x "$CHROME_PATH" ]]; then
  echo "Chrome executable not found: $CHROME_PATH" >&2
  exit 1
fi

cleanup() {
  if [[ -n "${WORKER_PID:-}" ]]; then
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

worker_args=(
  "$ROOT/scripts/image_sync/atlas_browser_worker.mjs"
  --chrome-path "$CHROME_PATH"
  --profile-dir "$PROFILE_DIR"
  --bind-host 127.0.0.1
  --port "$PORT"
  --token "$TOKEN"
)
if [[ -n "$PROBE_URL" ]]; then
  worker_args+=(--probe-url "$PROBE_URL")
fi

node "${worker_args[@]}" >"$LOG_FILE" 2>&1 &
WORKER_PID=$!

for _ in $(seq 1 120); do
  if curl --silent --show-error --fail --max-time 2 \
      -H "Authorization: Bearer $TOKEN" \
      "http://127.0.0.1:${PORT}/health" >/dev/null; then
    break
  fi
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "browser worker exited; see $LOG_FILE" >&2
    exit 1
  fi
  sleep 0.5
done

if ! curl --silent --show-error --fail --max-time 2 \
    -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo "browser worker did not become healthy; see $LOG_FILE" >&2
  exit 1
fi

if [[ -n "$PROBE_URL" ]]; then
  echo "[atlas-bootstrap] probing Atlas source before sync" >&2
  curl --silent --show-error --fail --max-time 90 \
    -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:${PORT}/probe" >&2
  echo >&2
fi

echo "[atlas-bootstrap] worker ready; starting standalone sync" >&2
python_args=(
  "$ROOT/scripts/image_sync/atlas_browser_sync.py"
  --source-file "$SOURCE_FILE"
  --bridge-url "http://127.0.0.1:${PORT}"
  --bridge-token "$TOKEN"
)
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$ROOT/venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT/venv/bin/python"
  elif [[ -x "/Users/mac/work/项目合集/aiops/venv/bin/python" ]]; then
    PYTHON_BIN="/Users/mac/work/项目合集/aiops/venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi
set +e
"$PYTHON_BIN" "${python_args[@]}" "$@"
STATUS=$?
set -e
exit "$STATUS"
