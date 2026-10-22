#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/venv/bin/python}"
WATCHDOG_SCRIPT="$PROJECT_ROOT/scripts/ops/runtime_watchdog.py"
CRON_MARKER="# aiops-runtime-watchdog"
CRON_CMD="0 */3 * * * cd $PROJECT_ROOT && ENV=test PYTHONPATH=. $PYTHON_BIN $WATCHDOG_SCRIPT > /dev/null 2>&1 $CRON_MARKER"

existing_crontab="$(mktemp)"
new_crontab="$(mktemp)"
trap 'rm -f "$existing_crontab" "$new_crontab"' EXIT

crontab -l 2>/dev/null | grep -v "$CRON_MARKER" > "$existing_crontab" || true
cat "$existing_crontab" > "$new_crontab"
echo "$CRON_CMD" >> "$new_crontab"
crontab "$new_crontab"

echo "Installed cron entry:"
echo "$CRON_CMD"
