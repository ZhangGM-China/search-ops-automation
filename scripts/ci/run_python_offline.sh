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
    echo "Python virtualenv not found. Set PYTHON_BIN to a project venv python." >&2
    exit 1
fi

export AIOPS_OFFLINE_TEST="1"
export PYTHONPATH="$PROJECT_ROOT/tests/offline_runtime:$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export NO_PROXY="localhost,127.0.0.1,0.0.0.0,::1"
export no_proxy="$NO_PROXY"

exec "$PYTHON_BIN" "$@"
