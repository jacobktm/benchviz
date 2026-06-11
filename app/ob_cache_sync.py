"""Sync OpenBenchmarking ob-cache from a local Phoronix Test Suite clone."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OB_CACHE_GITHUB = "https://github.com/phoronix-test-suite/phoronix-test-suite.git"
# Legacy dev-machine path; auto mode prefers instance/phoronix-test-suite under the project.
LEGACY_LOCAL_CLONE = "/home/system76/Git/phoronix-test-suite"
DEFAULT_BRANCH = "master"
PTS_UPDATE_TIMEOUT_SEC = 3600


def project_root() -> Path:
    return Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_pts_clone_dir(project_root_path: str | Path | None = None) -> Path:
    """Full PTS git checkout used for ob-cache updates (under instance/)."""
    root = Path(project_root_path) if project_root_path else project_root()
    env = os.environ.get("BENCHVIZ_PTS_CLONE_DIR", "").strip()
    if env:
        return Path(env)
    return root / "instance" / "phoronix-test-suite"


def default_ob_cache_dir(project_root_path: str | Path | None = None) -> Path:
    root = Path(project_root_path) if project_root_path else project_root()
    return root / "instance" / "ob-cache"


def default_index_path(project_root_path: str | Path | None = None) -> Path:
    root = Path(project_root_path) if project_root_path else project_root()
    return root / "instance" / "ob_cache_index.json"


def pts_executable(clone_dir: Path) -> Path:
    return clone_dir / "phoronix-test-suite"


def _run(cmd: list[str], cwd: Path | None = None, *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def ensure_pts_clone(
    clone_dir: Path | None = None,
    *,
    branch: str = DEFAULT_BRANCH,
) -> dict[str, Any]:
    """
    Clone or fast-forward the full Phoronix Test Suite repository to a stable path.

    Returns metadata including whether the tree was cloned or updated.
    """
    dest = Path(clone_dir or default_pts_clone_dir())
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {
        "clone_dir": str(dest),
        "branch": branch,
    }

    if (dest / ".git").is_dir():
        _run(["git", "fetch", "--depth", "1", "origin", branch], cwd=dest)
        _run(["git", "checkout", "FETCH_HEAD"], cwd=dest)
        meta["action"] = "updated"
    elif dest.is_dir() and any(dest.iterdir()):
        raise FileExistsError(
            f"{dest} exists but is not a git checkout; remove it or set BENCHVIZ_PTS_CLONE_DIR"
        )
    else:
        if dest.exists():
            shutil.rmtree(dest)
        _run([
            "git", "clone", "--depth", "1", "--branch", branch,
            OB_CACHE_GITHUB, str(dest),
        ])
        meta["action"] = "cloned"

    meta["has_ob_cache"] = (dest / "ob-cache" / "test-profiles").is_dir()
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    return meta


def run_pts_default_update(clone_dir: Path | None = None) -> dict[str, Any]:
    """
    Run ``phoronix-test-suite`` with no sub-command (PTS default).

    Startup refreshes OpenBenchmarking repository lists before the default help
    command runs. Requires PHP on PATH.
    """
    root = Path(clone_dir or default_pts_clone_dir())
    exe = pts_executable(root)
    meta: dict[str, Any] = {"clone_dir": str(root), "command": str(exe)}

    if not exe.is_file():
        meta["skipped"] = True
        meta["reason"] = "phoronix-test-suite script not found"
        return meta

    env = os.environ.copy()
    env.setdefault("PTS_SILENT_MODE", "1")

    try:
        proc = subprocess.run(
            [str(exe)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=PTS_UPDATE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        meta["ok"] = False
        meta["reason"] = f"timed out after {PTS_UPDATE_TIMEOUT_SEC}s"
        meta["stdout"] = (exc.stdout or "")[-4000:] if exc.stdout else ""
        meta["stderr"] = (exc.stderr or "")[-4000:] if exc.stderr else ""
        return meta

    meta["ok"] = proc.returncode == 0
    meta["returncode"] = proc.returncode
    if proc.stdout:
        meta["stdout_tail"] = proc.stdout[-4000:]
    if proc.stderr:
        meta["stderr_tail"] = proc.stderr[-4000:]
    if proc.returncode != 0 and "reason" not in meta:
        meta["reason"] = f"exit code {proc.returncode}"
    meta["ran_at"] = datetime.now(timezone.utc).isoformat()
    return meta


def _resolve_local_clone(local_path: str | Path | None) -> Path:
    if local_path:
        return Path(local_path)
    preferred = default_pts_clone_dir()
    if preferred.is_dir() and (preferred / "ob-cache").is_dir():
        return preferred
    legacy = Path(LEGACY_LOCAL_CLONE)
    if legacy.is_dir() and (legacy / "ob-cache").is_dir():
        return legacy
    return preferred


def sync_ob_cache(
    dest_dir: Path | None = None,
    *,
    source: str = "auto",
    local_path: str | Path | None = None,
    branch: str = DEFAULT_BRANCH,
    ensure_clone: bool = True,
    run_pts_update: bool = False,
) -> dict[str, Any]:
    """
    Copy ob-cache/test-profiles/**/generated.json into instance/ob-cache/.

    source:
      - auto: ensure instance/phoronix-test-suite (or BENCHVIZ_PTS_CLONE_DIR), else legacy path
      - local: require local_path (or default clone dir after ensure)
      - github: same as auto (full clone at the default path; sparse checkout removed)
    """
    dest = Path(dest_dir or default_ob_cache_dir())
    dest.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"dest": str(dest), "branch": branch}

    use_github_flow = source.lower() in ("auto", "github")
    local = _resolve_local_clone(local_path)

    if ensure_clone and use_github_flow:
        meta["clone"] = ensure_pts_clone(local, branch=branch)
        if run_pts_update:
            meta["pts_update"] = run_pts_default_update(local)

    use_local = (
        source.lower() == "local"
        or use_github_flow
        or (source.lower() == "auto" and local.is_dir() and (local / "ob-cache").is_dir())
    )

    if use_local:
        if not local.is_dir() or not (local / "ob-cache").is_dir():
            raise FileNotFoundError(
                f"No ob-cache/ under {local}. Run sync with ensure_clone or install PTS there."
            )
        src_root = local / "ob-cache" / "test-profiles"
        if not src_root.is_dir():
            raise FileNotFoundError(f"No ob-cache/test-profiles under {local}")
        copied = _copy_generated_json_files(src_root, dest / "test-profiles")
        meta.update({"source": "local", "local_path": str(local), "files_copied": copied})
    else:
        raise ValueError(f"Unsupported source {source!r}; use auto, local, or github")

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
        "pts_clone_dir": str(default_pts_clone_dir()),
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
