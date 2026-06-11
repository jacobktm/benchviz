#!/usr/bin/env bash
set -euo pipefail

# Install a systemd timer that periodically refreshes OpenBenchmarking ob-cache data.
# Usage (from project root):
#   chmod +x install_systemd_ob_cache_timer.sh
#   ./install_systemd_ob_cache_timer.sh

PROJECT_ROOT_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${BENCHVIZ_PROJECT_ROOT:-}" ]; then
  PROJECT_ROOT="$BENCHVIZ_PROJECT_ROOT"
else
  read -r -p "Path where BenchViz is installed [${PROJECT_ROOT_DEFAULT}]: " PROJECT_ROOT
  PROJECT_ROOT="${PROJECT_ROOT:-$PROJECT_ROOT_DEFAULT}"
fi

if [ ! -f "$PROJECT_ROOT/app_main.py" ]; then
  echo "No app_main.py found in '$PROJECT_ROOT'. Aborting."
  exit 1
fi

SERVICE_USER="${BENCHVIZ_DEFAULT_SERVICE_USER:-}"
if [ -z "$SERVICE_USER" ]; then
  SERVICE_USER="$(stat -c '%U' "$PROJECT_ROOT" 2>/dev/null || true)"
fi
if [ -z "$SERVICE_USER" ]; then
  SERVICE_USER="${SUDO_USER:-$USER}"
fi

if [ -n "${BENCHVIZ_DEFAULT_SERVICE_USER:-}" ]; then
  SERVICE_USER="$BENCHVIZ_DEFAULT_SERVICE_USER"
else
  read -r -p "System user to run the sync job as [${SERVICE_USER}]: " INPUT_USER
  SERVICE_USER="${INPUT_USER:-$SERVICE_USER}"
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  echo "User '${SERVICE_USER}' does not exist. Aborting."
  exit 1
fi

PYTHON_BIN="${PROJECT_ROOT}/venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "Expected virtualenv Python at ${PYTHON_BIN}. Run ./setup.sh first."
  exit 1
fi

INTERVAL_HOURS="${BENCHVIZ_OB_SYNC_INTERVAL_HOURS:-}"
if [ -z "$INTERVAL_HOURS" ]; then
  read -r -p "Sync interval in hours [24]: " INTERVAL_HOURS
  INTERVAL_HOURS="${INTERVAL_HOURS:-24}"
fi
if ! [[ "$INTERVAL_HOURS" =~ ^[0-9]+$ ]] || [ "$INTERVAL_HOURS" -lt 1 ]; then
  echo "Interval must be a positive integer (hours)."
  exit 1
fi

SERVICE_NAME="benchviz-sync-ob-cache"
FLASK_APP="${PROJECT_ROOT}/app_main.py"

SERVICE_UNIT="[Unit]
Description=Refresh BenchViz OpenBenchmarking cache from Phoronix Test Suite
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_ROOT}
Environment=PYTHONUNBUFFERED=1
Environment=FLASK_APP=${FLASK_APP}
ExecStart=${PYTHON_BIN} -m flask sync-openbenchmarking-cache
"

TIMER_UNIT="[Unit]
Description=Periodic OpenBenchmarking cache refresh for BenchViz

[Timer]
OnBootSec=15min
OnUnitActiveSec=${INTERVAL_HOURS}h
Persistent=true

[Install]
WantedBy=timers.target
"

echo "Installing ${SERVICE_NAME}.service and ${SERVICE_NAME}.timer ..."
echo "$SERVICE_UNIT" | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null
echo "$TIMER_UNIT" | sudo tee "/etc/systemd/system/${SERVICE_NAME}.timer" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.timer"
sudo systemctl restart "${SERVICE_NAME}.timer"

echo
echo "OpenBenchmarking sync timer installed (every ${INTERVAL_HOURS}h)."
echo "  sudo systemctl status ${SERVICE_NAME}.timer"
echo "  sudo systemctl start ${SERVICE_NAME}.service   # run once now"
echo
echo "Requires git and php for phoronix-test-suite (e.g. sudo apt install git php-cli)."
