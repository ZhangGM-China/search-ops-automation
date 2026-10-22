#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DEFAULT_SHARED_PYTHON="/Users/mac/work/项目合集/aiops/venv/bin/python"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/venv/bin/python}"

if [ ! -x "$PYTHON_BIN" ] && [ -x "$DEFAULT_SHARED_PYTHON" ]; then
    PYTHON_BIN="$DEFAULT_SHARED_PYTHON"
fi
if [ ! -x "$PYTHON_BIN" ]; then
    echo "❌ Python virtualenv not found. Set PYTHON_BIN to a project venv python."
    exit 1
fi

cd "$PROJECT_ROOT"

echo "🔐 Checking Git-tracked files for literal credentials..."
"$PYTHON_BIN" scripts/ci/check_tracked_credentials.py

echo "🔎 Checking external OpenAPI contract drift..."
ENV=test AUTO_DB_URI_TEST=sqlite:///:memory: OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" \
    "$PYTHON_BIN" scripts/ci/check_api_contract.py

echo "🔎 Checking API inventory and topic documentation coverage..."
"$PYTHON_BIN" scripts/ci/check_api_documentation.py

echo "🔎 Collecting read-only dead-code audit evidence..."
"$PYTHON_BIN" scripts/ci/audit_dead_code.py >/dev/null

SCHEDULER_CHECK_STATUS=0
SCHEDULER_CHECK_OUTPUT="$(bash scripts/ci/check_background_scheduler.sh)" || SCHEDULER_CHECK_STATUS=$?
printf '%s\n' "$SCHEDULER_CHECK_OUTPUT"
if [ "$SCHEDULER_CHECK_STATUS" -ne 0 ]; then
    echo "❌ Release Gate blocked by background scheduler detection."
    exit "$SCHEDULER_CHECK_STATUS"
fi

RELEASE_GATE_CLOSED=1
case "$SCHEDULER_CHECK_OUTPUT" in
    *NON-CLOSED*) RELEASE_GATE_CLOSED=0 ;;
esac

echo "🚀 Running automation full gate..."
ENV=test OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" bash scripts/ci/test_all.sh

echo "🔎 Running safe verify scripts..."
ENV=test OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" bash scripts/ci/run_python_offline.sh tests/manual_scripts/verification_scripts/verify_runtime_watchdog.py
ENV=test AUTO_DB_URI_TEST=sqlite:///:memory: OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" bash scripts/ci/run_python_offline.sh tests/manual_scripts/verification_scripts/verify_business_http_governor.py
if [ "$RELEASE_GATE_CLOSED" -eq 1 ]; then
    echo "✅ Release gate passed."
else
    echo "⚠️ NON-CLOSED validation completed under scheduler override; this is not a passed Release Gate."
    exit 1
fi
