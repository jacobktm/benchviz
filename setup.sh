#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$PROJECT_ROOT"

echo
read -r -p "Is this a system-wide/service install (you plan to run BenchViz via systemd)? [y/N]: " INSTALL_AS_SERVICE
INSTALL_AS_SERVICE="${INSTALL_AS_SERVICE:-N}"

INSTALL_ROOT="$PROJECT_ROOT"

if [[ "$INSTALL_AS_SERVICE" =~ ^[Yy]$ ]]; then
  if [ "$EUID" -ne 0 ]; then
    echo "For a service install, please re-run this script with sudo so it can set up systemd cleanly:"
    echo "  sudo ./setup.sh"
    exit 1
  fi

  # Ask where BenchViz should be installed for the service
  DEFAULT_INSTALL_ROOT="/opt/benchviz"
  read -r -p "Target install path for BenchViz [${DEFAULT_INSTALL_ROOT}]: " INSTALL_ROOT
  INSTALL_ROOT="${INSTALL_ROOT:-$DEFAULT_INSTALL_ROOT}"

  echo "Installing BenchViz files to '$INSTALL_ROOT'..."
  mkdir -p "$INSTALL_ROOT"

  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude 'venv' \
      --exclude '.git' \
      --exclude 'instance/benchmarks.db' \
      --exclude 'benchmarks/*.xml' \
      "$PROJECT_ROOT"/ "$INSTALL_ROOT"/
  else
    cp -a "$PROJECT_ROOT"/. "$INSTALL_ROOT"/
    rm -rf "$INSTALL_ROOT/venv" "$INSTALL_ROOT/.git"
    rm -f "$INSTALL_ROOT/instance/benchmarks.db" 2>/dev/null || true
    rm -f "$INSTALL_ROOT"/benchmarks/*.xml 2>/dev/null || true
  fi

  PROJECT_ROOT="$INSTALL_ROOT"
  cd "$PROJECT_ROOT"
fi

# Ensure python3-pip is available
if ! command -v pip3 >/dev/null 2>&1; then
  echo "python3-pip is not installed."
  echo "Please install it, for example on Ubuntu/Debian:"
  echo "  sudo apt install python3-pip"
  exit 1
fi

# Ensure python3-venv is available
if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "python3-venv is not installed or Python's venv module is unavailable."
  echo "Please install it, for example on Ubuntu/Debian:"
  echo "  sudo apt install python3-venv"
  exit 1
fi

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

echo "Environment ready. To run the app:"
echo "  cd \"$PROJECT_ROOT\""
echo "  source venv/bin/activate"
echo "  python app_main.py"

if [[ "$INSTALL_AS_SERVICE" =~ ^[Yy]$ ]]; then
  echo
  echo "Installing BenchViz as a systemd service..."
  if [ -x "./install_systemd_service.sh" ]; then
      # If the install root already exists and is owned by a non-root user,
      # prefer that owner as the default service user.
      BENCHVIZ_DEFAULT_SERVICE_USER=""
      if [ -d "$PROJECT_ROOT" ]; then
        dir_owner="$(stat -c '%U' "$PROJECT_ROOT" 2>/dev/null || true)"
        if [[ -n "${dir_owner:-}" && "${dir_owner}" != "root" ]]; then
          BENCHVIZ_DEFAULT_SERVICE_USER="$dir_owner"
        fi
      fi

      BENCHVIZ_DEFAULT_SERVICE_USER="$BENCHVIZ_DEFAULT_SERVICE_USER" ./install_systemd_service.sh
  else
      echo "install_systemd_service.sh not found or not executable. Skipping systemd setup."
  fi
else
  echo
  echo "Skipping systemd service installation."
fi

