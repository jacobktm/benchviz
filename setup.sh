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
  SERVICE_USER="benchviz"

  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "Creating service user '${SERVICE_USER}'..."
    if command -v adduser >/dev/null 2>&1; then
      adduser --disabled-password --gecos "" "${SERVICE_USER}"
    else
      useradd -m -s /bin/bash "${SERVICE_USER}"
    fi
  fi

  echo "Installing BenchViz files to '$INSTALL_ROOT' (owner: ${SERVICE_USER})..."
  mkdir -p "$INSTALL_ROOT"
  chown "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_ROOT"

  RSYNC_CHOWN=()
  if rsync --help 2>&1 | grep -q -- '--chown'; then
    RSYNC_CHOWN=(--chown="${SERVICE_USER}:${SERVICE_USER}")
  fi

  if command -v rsync >/dev/null 2>&1; then
    rsync -a "${RSYNC_CHOWN[@]}" \
      --exclude 'venv' \
      --exclude '.git' \
      --exclude 'instance/benchmarks.db' \
      --exclude 'benchmarks/*.xml' \
      "$PROJECT_ROOT"/ "$INSTALL_ROOT"/
    if [ "${#RSYNC_CHOWN[@]}" -eq 0 ]; then
      chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_ROOT"
    fi
  else
    cp -a "$PROJECT_ROOT"/. "$INSTALL_ROOT"/
    rm -rf "$INSTALL_ROOT/venv" "$INSTALL_ROOT/.git"
    rm -f "$INSTALL_ROOT/instance/benchmarks.db" 2>/dev/null || true
    rm -f "$INSTALL_ROOT"/benchmarks/*.xml 2>/dev/null || true
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_ROOT"
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
  if [[ "${INSTALL_AS_SERVICE:-N}" =~ ^[Yy]$ ]]; then
    sudo -u "${SERVICE_USER}" python3 -m venv venv
  else
    python3 -m venv venv
  fi
fi

if [[ "${INSTALL_AS_SERVICE:-N}" =~ ^[Yy]$ ]]; then
  sudo -u "${SERVICE_USER}" "$PROJECT_ROOT/venv/bin/pip" install --upgrade pip
  sudo -u "${SERVICE_USER}" "$PROJECT_ROOT/venv/bin/pip" install -r requirements.txt
else
  source venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt
fi

echo "Environment ready. To run the app:"
echo "  cd \"$PROJECT_ROOT\""
echo "  source venv/bin/activate"
echo "  python app_main.py"

if [[ "$INSTALL_AS_SERVICE" =~ ^[Yy]$ ]]; then
  echo
  echo "=== System-wide service setup ==="

  # Runtime deps for Phoronix Test Suite ob-cache sync (git clone + phoronix-test-suite).
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing git and php-cli (OpenBenchmarking cache sync)..."
    apt-get install -y git php-cli
  else
    missing=()
    command -v git >/dev/null 2>&1 || missing+=("git")
    command -v php >/dev/null 2>&1 || missing+=("php-cli")
    if [ "${#missing[@]}" -gt 0 ]; then
      echo "Missing packages: ${missing[*]}"
      echo "Install them before continuing (e.g. on Debian/Ubuntu: sudo apt install git php-cli)."
      exit 1
    fi
  fi

  echo
  echo "Installing BenchViz as a systemd service..."
  if [ ! -x "./install_systemd_service.sh" ]; then
    echo "install_systemd_service.sh not found or not executable. Aborting service setup."
    exit 1
  fi

  BENCHVIZ_PROJECT_ROOT="$PROJECT_ROOT" \
    BENCHVIZ_DEFAULT_SERVICE_USER="$SERVICE_USER" \
    BENCHVIZ_NONINTERACTIVE=1 \
    BENCHVIZ_SKIP_OB_TIMER=1 \
    ./install_systemd_service.sh

  echo
  echo "Seeding OpenBenchmarking cache (clone PTS, run phoronix-test-suite, build index)..."
  # Prior failed runs may have left root-owned files under instance/ (git dubious ownership).
  mkdir -p "${PROJECT_ROOT}/instance"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${PROJECT_ROOT}/instance"
  if ! sudo -u "${SERVICE_USER}" bash -c "
    cd \"${PROJECT_ROOT}\" &&
    export FLASK_APP=\"${PROJECT_ROOT}/app_main.py\" &&
    \"${PROJECT_ROOT}/venv/bin/python\" -m flask sync-openbenchmarking-cache
  "; then
    echo "Warning: OpenBenchmarking cache seed failed (check git/network/php)."
    echo "You can retry after setup with:"
    echo "  sudo -u ${SERVICE_USER} bash -c 'cd ${PROJECT_ROOT} && FLASK_APP=app_main.py venv/bin/python -m flask sync-openbenchmarking-cache'"
  fi

  echo
  echo "Installing periodic OpenBenchmarking cache sync timer (every 24h)..."
  BENCHVIZ_PROJECT_ROOT="$PROJECT_ROOT" \
    BENCHVIZ_DEFAULT_SERVICE_USER="$SERVICE_USER" \
    BENCHVIZ_OB_SYNC_INTERVAL_HOURS="24" \
    ./install_systemd_ob_cache_timer.sh

  echo
  echo "Service setup complete."
  echo "  sudo systemctl status benchviz.service"
  echo "  sudo systemctl status benchviz-sync-ob-cache.timer"
  echo "  sudo systemctl start benchviz-sync-ob-cache.service   # manual refresh"
else
  echo
  echo "Skipping systemd service installation."
fi

