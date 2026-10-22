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

echo "🔎 Enforcing zero undefined Python names (Flake8 F821)..."
cd "$PROJECT_ROOT"
PYTHONPATH=. bash scripts/ci/run_python_offline.sh -m flake8 . --select=F821 \
    --exclude=frontend,node_modules,.git,venv,.venv,src/top_query

echo "🐍 Running backend test suite with coverage using $PYTHON_BIN"
cd "$PROJECT_ROOT"
ENV=test OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" PYTHONPATH=. \
    bash scripts/ci/run_python_offline.sh -m pytest tests/ \
    --cov=src/query_governance \
    --cov=src/api/application/query_governance.py \
    --cov=src/api/contracts.py \
    --cov-report=term-missing \
    --cov-fail-under=80
