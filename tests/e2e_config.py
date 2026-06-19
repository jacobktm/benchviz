"""E2E test configuration — reads from a gitignored JSON file."""

import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "e2e_config.json")
_DEFAULT_URL = "http://127.0.0.1:8765"


def get_server_url() -> str:
    if not os.path.exists(_CONFIG_PATH):
        return _DEFAULT_URL
    with open(_CONFIG_PATH) as f:
        cfg = json.load(f)
    return cfg.get("server_url", _DEFAULT_URL)
