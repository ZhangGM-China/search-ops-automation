#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FOUND_PIDS=""
FOUND_DETAILS=""

while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    cwd="$(
        lsof -a -p "$pid" -d cwd -Fn 2>/dev/null \
            | sed -n 's/^n//p' \
            || true
    )"
    [ -n "$cwd" ] || continue
    if [ "$cwd" = "$PROJECT_ROOT" ]; then
        FOUND_PIDS="${FOUND_PIDS}${FOUND_PIDS:+ }${pid}"
        FOUND_DETAILS="${FOUND_DETAILS}PID: ${pid} | cwd: ${cwd}\n"
    fi
done < <(pgrep -f '[Pp]ython.*main.py' 2>/dev/null || true)

if [ -n "$FOUND_PIDS" ]; then
    echo "WARNING: background aiops scheduler main.py is running."
    printf '%b' "$FOUND_DETAILS"
    echo "risk: scheduler activity can mutate test observations, so this is not a closed Release Gate."
    if [ "${AIOPS_RELEASE_GATE_ALLOW_BACKGROUND_SCHEDULER:-0}" = "1" ]; then
        echo "NON-CLOSED: explicit override AIOPS_RELEASE_GATE_ALLOW_BACKGROUND_SCHEDULER=1 is active."
        exit 0
    fi
    echo "BLOCKED: stop or isolate the scheduler before running the Release Gate."
    exit 1
else
    echo "No background aiops scheduler detected."
fi
