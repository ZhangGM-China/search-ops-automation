#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_ROOT/logs"
cd "$PROJECT_ROOT"
mkdir -p "$LOG_DIR"

export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}."
export ENV="${ENV:-test}"
export TOP_QUERY_PLANNER_TIMEOUT_SECONDS="${TOP_QUERY_PLANNER_TIMEOUT_SECONDS:-5}"
export TOP_QUERY_WORLD_KNOWLEDGE_TIMEOUT_SECONDS="${TOP_QUERY_WORLD_KNOWLEDGE_TIMEOUT_SECONDS:-8}"

echo "Project root: $PROJECT_ROOT"

API_PORT="${API_PORT:-8014}"
FRONTEND_PORT="${FRONTEND_PORT:-5174}"
PI_RUNTIME_PORT="${PI_RUNTIME_PORT:-8124}"

if lsof -tiTCP:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "API already listening on port $API_PORT, skip start."
else
  nohup ./venv/bin/python -m uvicorn src.api:app --host 0.0.0.0 --port "$API_PORT" --no-access-log > "$LOG_DIR/api.log" 2>&1 &
  echo "API starting on port $API_PORT..."
fi

if lsof -tiTCP:"$PI_RUNTIME_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "PI runtime already listening on port $PI_RUNTIME_PORT, skip start."
else
  nohup env PI_RUNTIME_PORT="$PI_RUNTIME_PORT" \
    PI_AGENT_INTERNAL_TOKEN="${PI_AGENT_INTERNAL_TOKEN:-local-pi-runtime}" \
    KG_AGENT_MCP_URL="${KG_AGENT_MCP_URL:-https://mcphub.example.test/mcp/kg_agent_mcp}" \
    KG_AGENT_MCP_ENVIRONMENT="${KG_AGENT_MCP_ENVIRONMENT:-production}" \
    node --env-file-if-exists="$PROJECT_ROOT/.env" \
    "$PROJECT_ROOT/pi-runtime/src/server.mjs" > "$LOG_DIR/pi-runtime.log" 2>&1 &
  echo "PI runtime starting on port $PI_RUNTIME_PORT..."
fi

if pgrep -f "[Pp]ython .*main.py" >/dev/null 2>&1; then
  echo "Scheduler already running for $PROJECT_ROOT, skip start."
else
  nohup ./venv/bin/python main.py > "$LOG_DIR/scheduler.log" 2>&1 &
  echo "Scheduler starting..."
fi

# 启动 Query Governance Frontend。5174 只能由当前 worktree frontend 占用。
echo "Frontend starting through scripts/dev/start_query_governance_frontend.sh on port $FRONTEND_PORT..."
nohup env FRONTEND_PORT="$FRONTEND_PORT" VITE_API_BASE="http://127.0.0.1:$API_PORT/api" \
  "$PROJECT_ROOT/scripts/dev/start_query_governance_frontend.sh" > "$LOG_DIR/frontend.log" 2>&1 &

echo "All services started in background."
