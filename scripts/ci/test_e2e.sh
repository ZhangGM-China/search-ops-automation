#!/bin/bash

set -euo pipefail

export NO_PROXY=localhost,127.0.0.1,0.0.0.0
export no_proxy=localhost,127.0.0.1,0.0.0.0

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DEFAULT_SHARED_PYTHON="/Users/mac/work/项目合集/aiops/venv/bin/python"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/venv/bin/python}"
BACKEND_PID=""
FRONTEND_PID=""
E2E_DB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aiops-e2e-db.XXXXXX")"
E2E_DB_PATH="$E2E_DB_DIR/search_ops_automation_e2e.db"
AUTO_DB_URI_TEST="sqlite:///$E2E_DB_PATH"
E2E_RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
E2E_ARTIFACT_ROOT="${E2E_ARTIFACT_ROOT:-$PROJECT_ROOT/test-results}"
E2E_RUN_DIR="$E2E_ARTIFACT_ROOT/e2e-runtime-$E2E_RUN_ID"
E2E_RUN_DIR_CREATED=0
BACKEND_LOG="$E2E_RUN_DIR/backend.log"
FRONTEND_LOG="$E2E_RUN_DIR/frontend.log"

cleanup_pid() {
    local pid="$1"
    if [ -z "$pid" ]; then
        return
    fi
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in {1..50}; do
            if ! kill -0 "$pid" 2>/dev/null; then
                break
            fi
            sleep 0.1
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    wait "$pid" 2>/dev/null || true
}

_is_owned_run_dir() {
    local candidate="$1"
    local candidate_parent=""
    local candidate_name=""
    if [ "$E2E_RUN_DIR_CREATED" -ne 1 ] || [ -z "$candidate" ] \
        || [ ! -d "$candidate" ] || [ -L "$candidate" ]; then
        return 1
    fi
    candidate_name="$(basename "$candidate")"
    if [ "$candidate_name" != "e2e-runtime-$E2E_RUN_ID" ]; then
        return 1
    fi
    candidate_parent="$(cd "$(dirname "$candidate")" && pwd -P)"
    [ "$candidate_parent" = "$E2E_ARTIFACT_ROOT" ] \
        && [ "$candidate" = "$E2E_ARTIFACT_ROOT/$candidate_name" ]
}

cleanup() {
    local exit_status="$?"
    trap - EXIT INT TERM
    echo "🧹 Cleaning up E2E services..."
    cleanup_pid "${FRONTEND_PID:-}"
    cleanup_pid "${BACKEND_PID:-}"
    rm -rf -- "$E2E_DB_DIR"
    if _is_owned_run_dir "$E2E_RUN_DIR"; then
        if [ "$exit_status" -eq 0 ]; then
            rm -rf -- "$E2E_RUN_DIR"
        else
            echo "❌ E2E runtime artifacts retained at $E2E_RUN_DIR"
        fi
    fi
    exit "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ ! -x "$PYTHON_BIN" ] && [ -x "$DEFAULT_SHARED_PYTHON" ]; then
    PYTHON_BIN="$DEFAULT_SHARED_PYTHON"
fi
if [ ! -x "$PYTHON_BIN" ]; then
    echo "❌ Python virtualenv not found. Set PYTHON_BIN to a project venv python."
    exit 1
fi

_find_free_port() {
    "$PYTHON_BIN" - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

_require_free_port() {
    local port="$1"
    "$PYTHON_BIN" - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        raise SystemExit(f"port {port} is unavailable: {exc}")
PY
}

BACKEND_PORT="${BACKEND_PORT:-$(_find_free_port)}"
FRONTEND_PORT="${FRONTEND_PORT:-$(_find_free_port)}"
while [ "$FRONTEND_PORT" = "$BACKEND_PORT" ]; do
    FRONTEND_PORT="$(_find_free_port)"
done
_require_free_port "$BACKEND_PORT"
_require_free_port "$FRONTEND_PORT"
mkdir -p "$E2E_ARTIFACT_ROOT"
E2E_ARTIFACT_ROOT="$(cd "$E2E_ARTIFACT_ROOT" && pwd -P)"
E2E_RUN_DIR="$E2E_ARTIFACT_ROOT/e2e-runtime-$E2E_RUN_ID"
BACKEND_LOG="$E2E_RUN_DIR/backend.log"
FRONTEND_LOG="$E2E_RUN_DIR/frontend.log"
mkdir -- "$E2E_RUN_DIR"
E2E_RUN_DIR_CREATED=1

if [ ! -x "$PROJECT_ROOT/frontend/node_modules/.bin/vite" ] \
    || [ ! -x "$PROJECT_ROOT/frontend/node_modules/.bin/playwright" ]; then
    echo "❌ Install locked frontend dependencies before testing." >&2
    exit 1
fi

echo "🌱 Seeding test database..."
cd "$PROJECT_ROOT"
PYTHONPATH=. ENV=test AUTO_DB_URI_TEST="$AUTO_DB_URI_TEST" bash scripts/ci/run_python_offline.sh scripts/db/seed_test_db.py --skip-metrics

echo "📡 Starting backend on $BACKEND_PORT..."
PYTHONPATH=. ENV=test AUTO_DB_URI_TEST="$AUTO_DB_URI_TEST" OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" \
    bash scripts/ci/run_python_offline.sh -m uvicorn src.api:app --host 127.0.0.1 --port "$BACKEND_PORT" --no-access-log > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

echo "⏳ Waiting for backend readiness..."
for i in {1..30}; do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "❌ backend process exited before readiness"
        cat "$BACKEND_LOG"
        exit 1
    fi
    if curl -s "http://127.0.0.1:$BACKEND_PORT/openapi.json" | grep -q "openapi"; then
        echo "✅ Backend is READY."
        break
    fi
    sleep 1
    if [ $i -eq 30 ]; then
        echo "❌ Backend timeout. Last log:"
        cat "$BACKEND_LOG"
        exit 1
    fi
done
BACKEND_LISTENER_PIDS="$(lsof -tiTCP:"$BACKEND_PORT" -sTCP:LISTEN 2>/dev/null || true)"
if ! printf '%s\n' "$BACKEND_LISTENER_PIDS" | grep -qx "$BACKEND_PID"; then
    echo "❌ Backend listener is not owned by the current E2E run."
    cat "$BACKEND_LOG"
    exit 1
fi

echo "💻 Starting frontend on $FRONTEND_PORT..."
cd "$PROJECT_ROOT/frontend"
echo "FRONTEND_CWD=$(pwd)" > "$FRONTEND_LOG"
echo "VITE_API_BASE=http://127.0.0.1:$BACKEND_PORT/api" >> "$FRONTEND_LOG"
VITE_API_BASE=http://127.0.0.1:$BACKEND_PORT/api \
    "$PROJECT_ROOT/frontend/node_modules/.bin/vite" --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort >> "$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

echo "⏳ Waiting for frontend readiness..."
for i in {1..30}; do
    if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "❌ frontend process exited before readiness"
        cat "$FRONTEND_LOG"
        exit 1
    fi
    if curl -s "http://127.0.0.1:$FRONTEND_PORT" | grep -qi "html"; then
        echo "✅ Frontend is READY."
        break
    fi
    sleep 1
    if [ $i -eq 30 ]; then
        echo "❌ Frontend timeout. Last log:"
        cat "$FRONTEND_LOG"
        exit 1
    fi
done

FRONTEND_COMMAND=$(ps -p "$FRONTEND_PID" -o command= 2>/dev/null || true)
FRONTEND_LISTENER=$(lsof -nP -iTCP:"$FRONTEND_PORT" -sTCP:LISTEN 2>/dev/null || true)
FRONTEND_LISTENER_PIDS=$(lsof -tiTCP:"$FRONTEND_PORT" -sTCP:LISTEN 2>/dev/null || true)
FRONTEND_LISTENER_CWDS=""
for pid in $FRONTEND_LISTENER_PIDS; do
    FRONTEND_LISTENER_CWDS="$FRONTEND_LISTENER_CWDS
$pid $(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
done
echo "Frontend process: $FRONTEND_COMMAND"
echo "Frontend listener:"
echo "$FRONTEND_LISTENER"
echo "Frontend listener cwds:$FRONTEND_LISTENER_CWDS"
if ! printf '%s\n' "$FRONTEND_LISTENER_PIDS" | grep -qx "$FRONTEND_PID"; then
    echo "❌ Frontend listener is not owned by the current E2E run."
    cat "$FRONTEND_LOG"
    exit 1
fi
if ! echo "$FRONTEND_LISTENER_CWDS" | grep -q "$PROJECT_ROOT/frontend"; then
    echo "❌ Frontend listener is not from expected worktree frontend: $PROJECT_ROOT/frontend"
    cat "$FRONTEND_LOG"
    exit 1
fi

echo "🎭 Running Playwright E2E..."
PLAYWRIGHT_BASE_URL=http://127.0.0.1:$FRONTEND_PORT \
VITE_API_BASE=http://127.0.0.1:$BACKEND_PORT/api \
    AIOPS_OFFLINE_TEST=1 \
    "$PROJECT_ROOT/frontend/node_modules/.bin/playwright" test
