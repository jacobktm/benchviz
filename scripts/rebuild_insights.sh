#!/usr/bin/env bash
# Rebuild legacy + ML insights. Safe to run while benchviz.service is up (SQLite WAL).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PROJECT_ROOT}/venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "Expected venv Python at ${PYTHON_BIN}. Run ./setup.sh first." >&2
  exit 1
fi

LOCKFILE="${PROJECT_ROOT}/instance/rebuild-insights.lock"
mkdir -p "$(dirname "$LOCKFILE")"
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "Insights rebuild already in progress; skipping."
  exit 0
fi

export FLASK_APP="${PROJECT_ROOT}/app_main.py"
export PYTHONUNBUFFERED=1

REBUILD_ARGS=()
if [ "${BENCHVIZ_INSIGHTS_REBUILD_FULL:-}" = "1" ]; then
  REBUILD_ARGS+=(--full)
fi

echo "[$(date -Is)] Starting scheduled insights rebuild (incremental by default)..."
nice -n 10 "$PYTHON_BIN" -m flask rebuild-all-insights "${REBUILD_ARGS[@]}"
echo "[$(date -Is)] Insights rebuild complete."
