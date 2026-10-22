#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "🚀 Starting layered unified test suite from $PROJECT_ROOT..."
cd "$PROJECT_ROOT"

ENV=test OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" bash "$PROJECT_ROOT/scripts/ci/test_backend.sh"
ENV=test OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" bash "$PROJECT_ROOT/scripts/ci/test_frontend.sh"
ENV=test OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" bash "$PROJECT_ROOT/scripts/ci/test_e2e.sh"

echo "✅ ALL AUTOMATION TESTS PASSED!"
