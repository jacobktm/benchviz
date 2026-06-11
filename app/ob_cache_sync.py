"""Sync OpenBenchmarking ob-cache from Phoronix Test Suite GitHub or a local clone."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OB_CACHE_GITHUB = "https://github.com/phoronix-test-suite/phoronix-test-suite.git"
DEFAULT_LOCAL_CLONE = "/home/system76/Git/phoronix-test-suite"


def default_ob_cache_dir(project_root: str | None = None) -> Path:
    root = project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Path(root) / "instance" / "ob-cache"


def default_index_path(project_root: str | None = None) -> Path:
    root = project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Path(root) / "instance" / "ob_cache_index.json"


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def sync_ob_cache(
    dest_dir: Path | None = None,
    *,
    source: str = "auto",
    local_path: str | Path | None = None,
    branch: str = "master",
) -> dict[str, Any]:
    """
    Copy ob-cache/test-profiles/**/generated.json into instance/ob-cache/.

    source:
      - auto: use local_path / DEFAULT_LOCAL_CLONE if present, else shallow git sparse checkout
      - local: require local_path
      - github: always git sparse checkout into dest parent
    """
    dest = Path(dest_dir or default_ob_cache_dir())
    dest.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"dest": str(dest), "branch": branch}

    local = Path(local_path) if local_path else Path(DEFAULT_LOCAL_CLONE)
    use_local = source == "local" or (source == "auto" and local.is_dir() and (local / "ob-cache").is_dir())

    if use_local:
        src_root = local / "ob-cache" / "test-profiles"
        if not src_root.is_dir():
            raise FileNotFoundError(f"No ob-cache/test-profiles under {local}")
        copied = _copy_generated_json_files(src_root, dest / "test-profiles")
        meta.update({"source": "local", "local_path": str(local), "files_copied": copied})
    else:
        repo_dir = dest.parent / "phoronix-test-suite-src"
        if (repo_dir / ".git").is_dir():
            _run(["git", "fetch", "--depth", "1", "origin", branch], cwd=repo_dir)
            _run(["git", "checkout", "FETCH_HEAD"], cwd=repo_dir)
        else:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            repo_dir.mkdir(parents=True, exist_ok=True)
            _run([
                "git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
                OB_CACHE_GITHUB, str(repo_dir),
            ])
            _run(["git", "sparse-checkout", "set", "ob-cache/test-profiles"], cwd=repo_dir)
        src_root = repo_dir / "ob-cache" / "test-profiles"
        copied = _copy_generated_json_files(src_root, dest / "test-profiles")
        meta.update({"source": "github", "repo_dir": str(repo_dir), "files_copied": copied})

    meta["synced_at"] = datetime.now(timezone.utc).isoformat()
    return meta


def _copy_generated_json_files(src_root: Path, dest_root: Path) -> int:
    if dest_root.exists():
        shutil.rmtree(dest_root)
    count = 0
    for src in src_root.rglob("generated.json"):
        rel = src.relative_to(src_root)
        out = dest_root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        count += 1
    return count


def _ob_median_from_percentiles(percentiles: list[Any]) -> float | None:
    """OB population median (percentiles[50]) used as PTS reference baseline."""
    if not percentiles or len(percentiles) < 51:
        return None
    try:
        m = float(percentiles[50])
    except (TypeError, ValueError):
        return None
    return m if m > 0 else None


def build_ob_cache_index(cache_dir: Path | None = None, index_path: Path | None = None) -> dict[str, Any]:
    """Walk mirrored generated.json files and build hash -> percentile lookup index."""
    cache = Path(cache_dir or default_ob_cache_dir())
    profiles = cache / "test-profiles"
    idx_path = Path(index_path or default_index_path())
    entries: dict[str, dict[str, Any]] = {}
    files_read = 0

    if profiles.is_dir():
        for gen_file in profiles.rglob("generated.json"):
            files_read += 1
            rel_profile = gen_file.parent.relative_to(profiles)
            test_profile = str(rel_profile).replace("\\", "/")
            try:
                data = json.loads(gen_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            overview = data.get("overview") or {}
            if not isinstance(overview, dict):
                continue
            for comp_hash, row in overview.items():
                if not isinstance(row, dict):
                    continue
                entries[str(comp_hash)] = {
                    "test_profile": test_profile,
                    "description": row.get("description") or "",
                    "unit": row.get("unit") or "",
                    "hib": bool(row.get("hib", 1)),
                    "samples": int(row.get("samples") or 0),
                    "percentiles": row.get("percentiles") or [],
                    "ob_median": _ob_median_from_percentiles(row.get("percentiles") or []),
                    "test_version": row.get("test_version") or "",
                    "app_version": row.get("app_version") or "",
                }

    payload = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "cache_dir": str(cache),
        "files_read": files_read,
        "entry_count": len(entries),
        "entries": entries,
    }
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def load_ob_cache_index(index_path: Path | None = None) -> dict[str, Any] | None:
    path = Path(index_path or default_index_path())
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def lookup_ob_entry(comparison_hash: str, index: dict[str, Any] | None = None) -> dict[str, Any] | None:
    idx = index if index is not None else load_ob_cache_index()
    if not idx:
        return None
    ent = (idx.get("entries") or {}).get(comparison_hash)
    return ent if isinstance(ent, dict) else None
