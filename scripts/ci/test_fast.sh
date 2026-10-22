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
    echo "❌ Python virtualenv not found. Set PYTHON_BIN to a project venv python." >&2
    exit 1
fi

FRONTEND_VITEST="$PROJECT_ROOT/frontend/node_modules/.bin/vitest"
FRONTEND_ESLINT="$PROJECT_ROOT/frontend/node_modules/.bin/eslint"
FRONTEND_VITE="$PROJECT_ROOT/frontend/node_modules/.bin/vite"
FRONTEND_PLAYWRIGHT="$PROJECT_ROOT/frontend/node_modules/.bin/playwright"
FRONTEND_BINARIES=(
    "$FRONTEND_VITEST"
    "$FRONTEND_ESLINT"
    "$FRONTEND_VITE"
    "$FRONTEND_PLAYWRIGHT"
)
MISSING_FRONTEND_BINARIES=()
for frontend_binary in "${FRONTEND_BINARIES[@]}"; do
    if [ ! -x "$frontend_binary" ]; then
        MISSING_FRONTEND_BINARIES+=("$(basename "$frontend_binary")")
    fi
done
if [ "${#MISSING_FRONTEND_BINARIES[@]}" -gt 0 ]; then
    echo "❌ frontend local dependencies are incomplete (missing: ${MISSING_FRONTEND_BINARIES[*]}). Run npm ci in frontend before the fast gate." >&2
    exit 1
fi

cd "$PROJECT_ROOT"
echo "⚡ Running the local, no-install fast test gate..."

echo "🔎 Checking tracked credentials..."
"$PYTHON_BIN" scripts/ci/check_tracked_credentials.py

echo "🔎 Collecting entrypoint and route contracts..."
"$PYTHON_BIN" scripts/ci/audit_dead_code.py --collect-entrypoints-only >/dev/null

echo "🐍 Running targeted Python contracts..."
FAST_CONTRACTS='ci_scripts_exist_for_layered_test_entrypoints or fast_gate_is_local_only_and_targets_contract_surfaces or backend_script_enforces_src_coverage_gate or frontend_script_enforces_coverage_lint_and_build or frontend_package_exposes_stable_test_entrypoints or e2e_script_uses_dynamic_ports_and_isolated_runtime_artifacts or test_all_delegates_to_layered_scripts_instead_of_inlining_everything or top_query_frontend_check_uses_current_feature_first_test_paths'
ENV=test OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" PYTHONPATH=. \
    bash scripts/ci/run_python_offline.sh -m pytest -q tests/test_ci_scripts_contract.py -k "$FAST_CONTRACTS"
ENV=test OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}" PYTHONPATH=. \
    bash scripts/ci/run_python_offline.sh -m pytest -q tests/test_project_hygiene_contract.py

echo "⚛️ Running frontend architecture contracts..."
cd "$PROJECT_ROOT/frontend"
"$FRONTEND_VITEST" run \
    src/test/architecture/test-placement.test.ts \
    src/test/architecture/large-ui-boundaries.test.ts

echo "✅ Fast test gate passed."
