#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
FRONTEND_PORT="${FRONTEND_PORT:-5174}"
VITE_API_BASE="${VITE_API_BASE:-http://127.0.0.1:8000/api}"

resolve_npm_bin() {
  if [ -n "${NPM_BIN:-}" ]; then
    if [ ! -x "$NPM_BIN" ]; then
      echo "Configured NPM_BIN is not executable: $NPM_BIN" >&2
      return 1
    fi
    printf '%s\n' "$NPM_BIN"
    return 0
  fi

  local detected_npm
  detected_npm="$(command -v npm 2>/dev/null || true)"
  if [ -n "$detected_npm" ]; then
    printf '%s\n' "$detected_npm"
    return 0
  fi

  local candidate
  for candidate in /usr/local/bin/npm /opt/homebrew/bin/npm; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "npm executable not found; set NPM_BIN or add npm to PATH." >&2
  return 1
}

NPM_BIN="$(resolve_npm_bin)"
NPM_DIR="$(dirname "$NPM_BIN")"

echo "Query Governance frontend project root: $PROJECT_ROOT"
echo "Query Governance frontend cwd: $FRONTEND_DIR"
echo "Query Governance frontend URL: http://127.0.0.1:$FRONTEND_PORT"
echo "Query Governance frontend API base: $VITE_API_BASE"

if lsof -tiTCP:"$FRONTEND_PORT" -sTCP:LISTEN >/tmp/search_ops_frontend_pids 2>/dev/null; then
  while read -r pid; do
    [ -z "$pid" ] && continue
    command="$(ps -p "$pid" -o command=)"
    echo "Port $FRONTEND_PORT listener: $pid $command"
    if [[ "$command" != *"$FRONTEND_DIR"* ]]; then
      echo "Port $FRONTEND_PORT is occupied by a different frontend cwd." >&2
      echo "Stop that process first, then rerun this script." >&2
      exit 1
    fi
  done < /tmp/search_ops_frontend_pids
  echo "Correct Query Governance frontend is already running."
  exit 0
fi

cd "$FRONTEND_DIR"
PATH="$NPM_DIR:$PATH" VITE_API_BASE="$VITE_API_BASE" "$NPM_BIN" run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort
