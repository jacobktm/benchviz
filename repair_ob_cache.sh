#!/usr/bin/env bash
# One-shot repair for a broken /opt/benchviz OpenBenchmarking cache (mangled pts-user* paths, empty ob-cache).
# Run on the server as root:
#   sudo ./repair_ob_cache.sh
set -euo pipefail

INSTALL_ROOT="${BENCHVIZ_INSTALL_ROOT:-/opt/benchviz}"
SERVICE_USER="${BENCHVIZ_SERVICE_USER:-benchviz}"

if [ "$EUID" -ne 0 ]; then
  echo "Run with sudo: sudo $0"
  exit 1
fi

if [ ! -f "${INSTALL_ROOT}/app_main.py" ]; then
  echo "BenchViz not found at ${INSTALL_ROOT}"
  exit 1
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  echo "User ${SERVICE_USER} does not exist."
  exit 1
fi

echo "Stopping any stuck OB sync / setup (benchviz user)..."
pkill -u "${SERVICE_USER}" -f 'flask sync-openbenchmarking-cache' 2>/dev/null || true
pkill -u "${SERVICE_USER}" -f 'phoronix-test-suite' 2>/dev/null || true
sleep 1

INSTANCE="${INSTALL_ROOT}/instance"
echo "Removing mangled PTS paths under ${INSTANCE}..."
rm -rf \
  "${INSTANCE}/pts-useropenbenchmarking.org" \
  "${INSTANCE}/pts-usertest-profiles" \
  "${INSTANCE}/pts-usertest-suites" \
  "${INSTANCE}/pts-userdownload-cache" \
  "${INSTANCE}/pts-usermodules" \
  "${INSTANCE}/pts-usermodules-data" \
  "${INSTANCE}/pts-usercore.pt2so" \
  "${INSTANCE}/pts-usergraph-config.json" \
  "${INSTANCE}/pts-useruser-config.xml" \
  "${INSTANCE}/pts-userresult_viewer_lock" \
  "${INSTANCE}"/pts-userrun-lock-* 2>/dev/null || true

mkdir -p "${INSTANCE}/pts-user" "${INSTANCE}/ob-cache"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTANCE}"

echo "Syncing ob-cache from git mirror (fast, no bulk live fetch)..."
sudo -u "${SERVICE_USER}" bash -c "
  cd '${INSTALL_ROOT}' &&
  export FLASK_APP='${INSTALL_ROOT}/app_main.py' &&
  '${INSTALL_ROOT}/venv/bin/python' -m flask sync-openbenchmarking-cache --skip-live-fetch
"

N="$(find "${INSTANCE}/ob-cache/test-profiles" -name generated.json 2>/dev/null | wc -l | tr -d ' ')"
echo
echo "Done. generated.json files under ob-cache: ${N}"
if [ "${N}" -lt 100 ]; then
  echo "Expected hundreds of files — check:"
  echo "  ls ${INSTANCE}/phoronix-test-suite/ob-cache/test-profiles | head"
  exit 1
fi
echo "Restart BenchViz if needed: sudo systemctl restart benchviz.service"
