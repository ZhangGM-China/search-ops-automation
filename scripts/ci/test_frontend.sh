#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "⚛️ Running frontend vitest coverage, lint, and build"
cd "$PROJECT_ROOT/frontend"
if [ ! -x node_modules/.bin/vitest ] \
    || [ ! -x node_modules/.bin/eslint ]; then
    echo "❌ Install locked frontend dependencies before testing." >&2
    exit 1
fi
npm run test:coverage
npm run lint
npm run build
