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

  BENCHVIZ_DEFAULT_SERVICE_USER=""
  if [ -d "$PROJECT_ROOT" ]; then
    dir_owner="$(stat -c '%U' "$PROJECT_ROOT" 2>/dev/null || true)"
    if [[ -n "${dir_owner:-}" && "${dir_owner}" != "root" ]]; then
      BENCHVIZ_DEFAULT_SERVICE_USER="$dir_owner"
    fi
  fi
  if [ -z "$BENCHVIZ_DEFAULT_SERVICE_USER" ]; then
    BENCHVIZ_DEFAULT_SERVICE_USER="${SUDO_USER:-benchviz}"
  fi

  if ! id -u "${BENCHVIZ_DEFAULT_SERVICE_USER}" >/dev/null 2>&1; then
    echo "Creating service user '${BENCHVIZ_DEFAULT_SERVICE_USER}'..."
    if command -v adduser >/dev/null 2>&1; then
      adduser --disabled-password --gecos "" "${BENCHVIZ_DEFAULT_SERVICE_USER}"
    else
      useradd -m -s /bin/bash "${BENCHVIZ_DEFAULT_SERVICE_USER}"
    fi
  fi

  BENCHVIZ_PROJECT_ROOT="$PROJECT_ROOT" \
    BENCHVIZ_DEFAULT_SERVICE_USER="$BENCHVIZ_DEFAULT_SERVICE_USER" \
    BENCHVIZ_NONINTERACTIVE=1 \
    BENCHVIZ_SKIP_OB_TIMER=1 \
    ./install_systemd_service.sh

  SERVICE_USER="$BENCHVIZ_DEFAULT_SERVICE_USER"

  echo
  echo "Seeding OpenBenchmarking cache (clone PTS, run phoronix-test-suite, build index)..."
  sudo -u "${SERVICE_USER}" bash -c "
    cd \"${PROJECT_ROOT}\" &&
    export FLASK_APP=\"${PROJECT_ROOT}/app_main.py\" &&
    \"${PROJECT_ROOT}/venv/bin/python\" -m flask sync-openbenchmarking-cache
  "

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

