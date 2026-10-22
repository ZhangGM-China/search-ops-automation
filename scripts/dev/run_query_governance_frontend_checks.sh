#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"

echo "Query Governance frontend check project root: $PROJECT_ROOT"
echo "Query Governance frontend check cwd: $FRONTEND_DIR"

cd "$FRONTEND_DIR"
if [ ! -x ./node_modules/.bin/vitest ]; then
  echo "Frontend test binaries are missing; run npm ci in $FRONTEND_DIR first." >&2
  exit 1
fi

./node_modules/.bin/vitest run \
  src/features/query-governance/components/QueryGovernanceMetricTrend.test.tsx \
  src/features/query-governance/components/BrandAssistantDrawer.test.tsx
npm run lint
npm run build
