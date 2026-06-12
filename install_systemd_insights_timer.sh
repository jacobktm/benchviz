#!/usr/bin/env bash
set -euo pipefail

# Install a systemd timer that periodically rebuilds performance + ML insights.
# Safe while benchviz.service is running (SQLite WAL + shared flock lock).
#
# Usage (from project root):
#   chmod +x install_systemd_insights_timer.sh
#   ./install_systemd_insights_timer.sh

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

REBUILD_SCRIPT="${PROJECT_ROOT}/scripts/rebuild_insights.sh"
if [ ! -f "$REBUILD_SCRIPT" ]; then
  echo "Missing ${REBUILD_SCRIPT}. Aborting."
  exit 1
fi
chmod +x "$REBUILD_SCRIPT"

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
  read -r -p "System user to run the rebuild job as [${SERVICE_USER}]: " INPUT_USER
  SERVICE_USER="${INPUT_USER:-$SERVICE_USER}"
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  echo "User '${SERVICE_USER}' does not exist. Aborting."
  exit 1
fi

INTERVAL_HOURS="${BENCHVIZ_INSIGHTS_REBUILD_INTERVAL_HOURS:-}"
if [ -z "$INTERVAL_HOURS" ]; then
  read -r -p "Insights rebuild interval in hours [1]: " INTERVAL_HOURS
  INTERVAL_HOURS="${INTERVAL_HOURS:-1}"
fi
if ! [[ "$INTERVAL_HOURS" =~ ^[0-9]+$ ]] || [ "$INTERVAL_HOURS" -lt 1 ]; then
  echo "Interval must be a positive integer (hours)."
  exit 1
fi

SERVICE_NAME="benchviz-rebuild-insights"

SERVICE_UNIT="[Unit]
Description=Rebuild BenchViz performance and ML insights
After=network-online.target benchviz.service
Wants=network-online.target

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_ROOT}
Environment=PYTHONUNBUFFERED=1
ExecStart=${REBUILD_SCRIPT}
"

TIMER_UNIT="[Unit]
Description=Periodic BenchViz insights rebuild (legacy cohort stats + ML profiles)

[Timer]
OnBootSec=10min
OnUnitActiveSec=${INTERVAL_HOURS}h
RandomizedDelaySec=300
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
echo "Insights rebuild timer installed (every ${INTERVAL_HOURS}h, safe while benchviz is running)."
echo "  sudo systemctl status ${SERVICE_NAME}.timer"
echo "  sudo systemctl start ${SERVICE_NAME}.service   # run once now"
echo "  journalctl -u ${SERVICE_NAME}.service -n 50     # recent rebuild logs"
