"""Sync OpenBenchmarking ob-cache from a local Phoronix Test Suite clone."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app._util import project_root

OB_CACHE_GITHUB = "https://github.com/phoronix-test-suite/phoronix-test-suite.git"
LEGACY_LOCAL_CLONE = "/home/system76/Git/phoronix-test-suite"
DEFAULT_BRANCH = "master"
PTS_UPDATE_TIMEOUT_SEC = 3600
LIVE_FETCH_TIMEOUT_SEC = 180
DEFAULT_OB_CACHE_TTL_HOURS = 168

ObLookupSource = str


def default_pts_clone_dir(project_root_path: str | Path | None = None) -> Path:
    root = Path(project_root_path) if project_root_path else Path(project_root())
    env = os.environ.get("BENCHVIZ_PTS_CLONE_DIR", "").strip()
    if env:
        return Path(env)
    return root / "instance" / "phoronix-test-suite"


def default_ob_cache_dir(project_root_path: str | Path | None = None) -> Path:
    root = Path(project_root_path) if project_root_path else Path(project_root())
    return root / "instance" / "ob-cache"


def default_index_path(project_root_path: str | Path | None = None) -> Path:
    root = Path(project_root_path) if project_root_path else Path(project_root())
    return root / "instance" / "ob_cache_index.json"


def default_pts_user_path(project_root_path: str | Path | None = None) -> Path:
    env = os.environ.get("BENCHVIZ_PTS_USER_PATH", "").strip()
    if env:
        return Path(env.rstrip("/"))
    root = Path(project_root_path) if project_root_path else Path(project_root())
    return root / "instance" / "pts-user"


def pts_user_path_override_value(project_root_path: str | Path | None = None) -> str:
    p = default_pts_user_path(project_root_path)
    s = str(p.resolve())
    return s if s.endswith("/") else f"{s}/"


def pts_executable(clone_dir: Path) -> Path:
    return clone_dir / "phoronix-test-suite"


def compare_ob_live_fetch_enabled() -> bool:
    return os.environ.get("BENCHVIZ_OB_LIVE_ON_COMPARE", "").strip().lower() in ("1", "true", "yes", "on")


def live_ob_fetch_enabled() -> bool:
    return os.environ.get("BENCHVIZ_OB_LIVE_FETCH", "0").strip().lower() in ("1", "true", "yes", "on")


def ob_cache_ttl_seconds() -> int:
    raw = os.environ.get("BENCHVIZ_OB_CACHE_TTL_HOURS", "").strip()
    if raw:
        try:
            hours = float(raw)
        except ValueError:
            hours = DEFAULT_OB_CACHE_TTL_HOURS
    else:
        hours = DEFAULT_OB_CACHE_TTL_HOURS
    if hours <= 0:
        return 0
    return int(hours * 3600)


def _cached_generated_json_path(
    test_profile: str,
    cache_dir: Path | None = None,
) -> Path:
    cache = Path(cache_dir or default_ob_cache_dir())
    return cache / "test-profiles" / test_profile / "generated.json"


def _is_cache_fresh(path: Path) -> bool:
    ttl = ob_cache_ttl_seconds()
    if ttl == 0:
        return True
    if not path.is_file():
        return False
    return (time.time() - path.stat().st_mtime) < ttl


def _index_entry_cache_fresh(entry: dict[str, Any]) -> bool:
    test_profile = entry.get("test_profile")
    if not test_profile:
        return True
    return _is_cache_fresh(_cached_generated_json_path(str(test_profile)))
