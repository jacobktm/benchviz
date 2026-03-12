#!/usr/bin/env bash
set -euo pipefail

# Simple helper to install a systemd service for BenchViz.
# Usage (from project root):
#   chmod +x install_systemd_service.sh
#   ./install_systemd_service.sh

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="benchviz"

DEFAULT_USER="${SUDO_USER:-$USER}"
read -r -p "System user to run BenchViz as [${DEFAULT_USER}]: " SERVICE_USER
SERVICE_USER="${SERVICE_USER:-$DEFAULT_USER}"

# Ensure the service user exists (print instructions if missing)
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  echo "User '${SERVICE_USER}' does not exist."
  echo
  echo "Please create this user before installing the systemd service. For example:"
  if command -v adduser >/dev/null 2>&1; then
      echo "  sudo adduser --disabled-password --gecos \"\" ${SERVICE_USER}"
  else
      echo "  sudo useradd -m -s /bin/bash ${SERVICE_USER}"
  fi
  echo
  echo "Then re-run ./install_systemd_service.sh."
  exit 1
fi

PYTHON_BIN="${PROJECT_ROOT}/venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "Expected virtualenv Python at ${PYTHON_BIN} but it was not found or not executable."
  echo "Run ./setup.sh first so the venv and dependencies are created."
  exit 1
fi

UNIT_FILE_CONTENT="[Unit]
Description=BenchViz Flask application
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_ROOT}
ExecStart=${PYTHON_BIN} app_main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"

echo "This script will write /etc/systemd/system/${SERVICE_NAME}.service and enable/start it."
echo "You will be prompted for sudo privileges."

echo "$UNIT_FILE_CONTENT" | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"

echo
echo "BenchViz systemd service installed and (re)started."
echo "Check status with:"
echo "  sudo systemctl status ${SERVICE_NAME}.service"

