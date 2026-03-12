#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$PROJECT_ROOT"

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
echo "  source venv/bin/activate"
echo "  python app_main.py"

