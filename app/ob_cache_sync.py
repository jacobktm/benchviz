"""Sync OpenBenchmarking ob-cache from a local Phoronix Test Suite clone."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app._util import project_root

from .pts_comparison import (
    generate_comparison_hash,
    hash_identifier_from_test_profile,
    normalize_ob_unit,
    parse_version_tuple,
    strip_test_profile_identifier,
    test_profile_family,
)

OB_CACHE_GITHUB = "https://github.com/phoronix-test-suite/phoronix-test-suite.git"
# Legacy dev-machine path; auto mode prefers instance/phoronix-test-suite under the project.
LEGACY_LOCAL_CLONE = "/home/system76/Git/phoronix-test-suite"
DEFAULT_BRANCH = "master"
PTS_UPDATE_TIMEOUT_SEC = 3600
LIVE_FETCH_TIMEOUT_SEC = 180
# Re-fetch generated.json from OpenBenchmarking after this many hours (PTS index ~3d; stats ~weekly).
DEFAULT_OB_CACHE_TTL_HOURS = 168
_index_write_lock = threading.Lock()
ObLookupSource = str  # "", "live", "local", "fallback"


def default_pts_clone_dir(project_root_path: str | Path | None = None) -> Path:
    """Full PTS git checkout used for ob-cache updates (under instance/)."""
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
    """PTS user dir for live OpenBenchmarking downloads (generated.json acquire)."""
    env = os.environ.get("BENCHVIZ_PTS_USER_PATH", "").strip()
    if env:
        return Path(env.rstrip("/"))
    root = Path(project_root_path) if project_root_path else Path(project_root())
    return root / "instance" / "pts-user"


def pts_user_path_override_value(project_root_path: str | Path | None = None) -> str:
    """
    Value for PTS_USER_PATH_OVERRIDE.

    Phoronix Test Suite concatenates subpaths without inserting '/' (expects a trailing slash).
    """
    p = default_pts_user_path(project_root_path)
    s = str(p.resolve())
    return s if s.endswith("/") else f"{s}/"


def pts_executable(clone_dir: Path) -> Path:
    return clone_dir / "phoronix-test-suite"


def compare_ob_live_fetch_enabled() -> bool:
    """Live OB fetch during /api/compare (default off — avoids multi-minute page loads)."""
    return os.environ.get("BENCHVIZ_OB_LIVE_ON_COMPARE", "").strip().lower() in ("1", "true", "yes", "on")


def live_ob_fetch_enabled() -> bool:
    """Live OB fetch during sync/supplement (default off; use --skip-live-fetch or timer)."""
    return os.environ.get("BENCHVIZ_OB_LIVE_FETCH", "0").strip().lower() in ("1", "true", "yes", "on")


def ob_cache_ttl_seconds() -> int:
    """
    Max age for a cached generated.json before live re-fetch.

    Override with BENCHVIZ_OB_CACHE_TTL_HOURS (float hours). Set to 0 to never expire.
    """
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


def _git_cmd_detail(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    if len(text) > 2000:
        return text[-2000:]
    return text


def _run(cmd: list[str], cwd: Path | None = None, *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = _git_cmd_detail(proc)
        msg = f"Command {cmd!r} failed with exit status {proc.returncode}"
        if detail:
            msg += f": {detail}"
        raise RuntimeError(msg)
    return proc


def _git_cmd(repo: Path | None, *args: str) -> list[str]:
    """Build git argv; mark repo as safe.directory when operating inside an existing clone."""
    cmd = ["git"]
    if repo is not None:
        cmd.extend(["-c", f"safe.directory={repo.resolve()}"])
    cmd.extend(args)
    return cmd


def _fresh_pts_clone(dest: Path, branch: str) -> None:
    if dest.exists():
        try:
            shutil.rmtree(dest)
        except PermissionError as exc:
            raise RuntimeError(
                f"Cannot remove {dest} (permission denied). "
                f"Fix ownership then retry, e.g.: "
                f"sudo chown -R $(stat -c '%U' {dest.parent}) {dest.parent} "
                f"or sudo rm -rf {dest}"
            ) from exc
    _run(_git_cmd(None, "clone", "--depth", "1", "--single-branch", "--branch", branch,
                   OB_CACHE_GITHUB, str(dest)))


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
        try:
            _run(_git_cmd(dest, "fetch", "origin", branch, "--depth", "1"), cwd=dest)
            _run(_git_cmd(dest, "reset", "--hard", "FETCH_HEAD"), cwd=dest)
            meta["action"] = "updated"
        except RuntimeError as exc:
            meta["fetch_error"] = str(exc)
            _fresh_pts_clone(dest, branch)
            meta["action"] = "recloned"
    elif dest.is_dir() and any(dest.iterdir()):
        raise FileExistsError(
            f"{dest} exists but is not a git checkout; remove it or set BENCHVIZ_PTS_CLONE_DIR"
        )
    else:
        _fresh_pts_clone(dest, branch)
        meta["action"] = "cloned"

    meta["has_ob_cache"] = (dest / "ob-cache" / "test-profiles").is_dir()
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    return meta


def run_pts_openbenchmarking_refresh(clone_dir: Path | None = None) -> dict[str, Any]:
    """Force-refresh OpenBenchmarking repo indexes (pts.index, etc.)."""
    root = Path(clone_dir or default_pts_clone_dir())
    exe = pts_executable(root)
    user_path = default_pts_user_path()
    user_path.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"clone_dir": str(root), "command": f"{exe} openbenchmarking-refresh"}

    if not exe.is_file():
        meta["skipped"] = True
        meta["reason"] = "phoronix-test-suite script not found"
        return meta

    env = os.environ.copy()
    env["PTS_USER_PATH_OVERRIDE"] = pts_user_path_override_value()
    env.setdefault("PTS_SILENT_MODE", "1")

    try:
        proc = subprocess.run(
            [str(exe), "openbenchmarking-refresh"],
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
        meta["stderr"] = (exc.stderr or "")[-4000:] if exc.stderr else ""
        return meta

    meta["ok"] = proc.returncode == 0
    meta["returncode"] = proc.returncode
    if proc.returncode != 0:
        meta["reason"] = f"exit code {proc.returncode}"
        if proc.stderr:
            meta["stderr_tail"] = proc.stderr[-4000:]
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
    live_fetch: bool = True,
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
            meta["pts_update"] = run_pts_openbenchmarking_refresh(local)

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

    if live_fetch and live_ob_fetch_enabled():
        meta["live_fetch"] = supplement_ob_cache_from_live(dest_dir=dest)

    if use_local:
        copied = _merge_copy_generated_json_files(
            src_root,
            dest / "test-profiles",
            skip_existing=True,
        )
        meta.update({"source": "local", "local_path": str(local), "files_copied": copied})
    else:
        raise ValueError(f"Unsupported source {source!r}; use auto, local, or github")

    meta["synced_at"] = datetime.now(timezone.utc).isoformat()
    return meta


def _merge_copy_generated_json_files(
    src_root: Path,
    dest_root: Path,
    *,
    skip_existing: bool = False,
) -> int:
    """Overlay generated.json files from src onto dest without removing existing profiles."""
    count = 0
    if not src_root.is_dir():
        return count
    for src in src_root.rglob("generated.json"):
        rel = src.relative_to(src_root)
        out = dest_root / rel
        if skip_existing and out.is_file():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        count += 1
    return count


def _copy_generated_json_files(src_root: Path, dest_root: Path) -> int:
    if dest_root.exists():
        shutil.rmtree(dest_root)
    return _merge_copy_generated_json_files(src_root, dest_root)


def _ob_median_from_percentiles(percentiles: list[Any]) -> float | None:
    """OB population median (percentiles[50]) used as PTS reference baseline."""
    return _ob_percentile_from_list(percentiles, 50)


def _ob_p1_from_percentiles(percentiles: list[Any]) -> float | None:
    """OB population baseline reference (percentiles[0] from generated.json)."""
    return _ob_percentile_from_list(percentiles, 0)


def _ob_percentile_from_list(percentiles: list[Any], index: int) -> float | None:
    if not percentiles or len(percentiles) <= index:
        return None
    try:
        v = float(percentiles[index])
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _entry_from_overview_row(test_profile: str, comp_hash: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "test_profile": test_profile,
        "description": row.get("description") or "",
        "unit": row.get("unit") or "",
        "hib": bool(row.get("hib", 1)),
        "samples": int(row.get("samples") or 0),
        "percentiles": row.get("percentiles") or [],
        "ob_median": _ob_median_from_percentiles(row.get("percentiles") or []),
        "ob_p1": _ob_p1_from_percentiles(row.get("percentiles") or []),
        "test_version": row.get("test_version") or "",
        "app_version": row.get("app_version") or "",
    }


def _entries_from_generated_json(test_profile: str, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    overview = data.get("overview") or {}
    if not isinstance(overview, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for comp_hash, row in overview.items():
        if isinstance(row, dict):
            out[str(comp_hash)] = _entry_from_overview_row(test_profile, str(comp_hash), row)
    return out


def _pts_repo_index_paths() -> list[Path]:
    paths: list[Path] = []
    user_idx = default_pts_user_path() / "openbenchmarking.org" / "pts.index"
    if user_idx.is_file():
        paths.append(user_idx)
    clone_idx = default_pts_clone_dir() / "ob-cache" / "openbenchmarking.org" / "pts.index"
    if clone_idx.is_file():
        paths.append(clone_idx)
    return paths


def _repo_test_name_from_identifier(identifier: str | None) -> tuple[str, str] | None:
    tp = strip_test_profile_identifier(identifier)
    if not tp:
        return None
    m = re.match(r"^([^/]+)/(.+?)-(?:\d+\.)+\d+$", tp)
    if not m:
        m = re.match(r"^([^/]+)/(.+)$", tp)
        if not m:
            return None
        repo, name = m.group(1), m.group(2)
        name = re.sub(r"-\d[\d.]*$", "", name)
        return repo, name
    return m.group(1), m.group(2)


def _sort_profile_versions_desc(versions: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for v in versions:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return sorted(unique, key=parse_version_tuple, reverse=True)


def list_disk_test_profiles_for_family(family: str, cache_dir: Path | None = None) -> list[str]:
    """Qualified profiles present under ob-cache for a benchmark family (newest first)."""
    fam = (family or "").strip().replace("\\", "/")
    if not fam:
        return []
    cache = Path(cache_dir or default_ob_cache_dir())
    profiles_root = cache / "test-profiles"
    if not profiles_root.is_dir():
        return []

    parts = fam.split("/")
    if len(parts) != 2:
        return []
    repo, name = parts
    parent = profiles_root / repo
    if not parent.is_dir():
        return []

    prefix = f"{name}-"
    found: list[str] = []
    for d in parent.iterdir():
        if not d.is_dir() or not d.name.startswith(prefix):
            continue
        if (d / "generated.json").is_file():
            found.append(f"{repo}/{d.name}")
    return sorted(found, key=_test_profile_version_tuple, reverse=True)


def list_test_profiles_for_identifier(identifier: str | None, cache_dir: Path | None = None) -> list[str]:
    """Qualified test profiles (newest first): PTS index plus any mirrored on disk."""
    parsed = _repo_test_name_from_identifier(identifier)
    profiles: list[str] = []
    if parsed:
        repo, test_name = parsed
        versions: list[str] = []
        for idx_path in _pts_repo_index_paths():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            test = (data.get("tests") or {}).get(test_name) or {}
            versions.extend(test.get("versions") or [])
        profiles.extend(f"{repo}/{test_name}-{v}" for v in _sort_profile_versions_desc(versions))

    tp = strip_test_profile_identifier(identifier)
    family = test_profile_family(tp) if tp else ""
    if family:
        profiles.extend(list_disk_test_profiles_for_family(family, cache_dir))

    seen: set[str] = set()
    unique: list[str] = []
    for p in profiles:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return sorted(unique, key=_test_profile_version_tuple, reverse=True)


def run_pts_fetch_test_profile(test_profile: str) -> dict[str, Any]:
    """Download a test profile from OpenBenchmarking.org via ``phoronix-test-suite info``."""
    root = default_pts_clone_dir()
    exe = pts_executable(root)
    user_path = default_pts_user_path()
    user_path.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"test_profile": test_profile, "clone_dir": str(root)}

    if not exe.is_file():
        meta["ok"] = False
        meta["reason"] = "phoronix-test-suite script not found"
        return meta

    env = os.environ.copy()
    env["PTS_USER_PATH_OVERRIDE"] = pts_user_path_override_value()
    env.setdefault("PTS_SILENT_MODE", "1")

    try:
        proc = subprocess.run(
            [str(exe), "info", test_profile],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=LIVE_FETCH_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        meta["ok"] = False
        meta["reason"] = f"timed out after {LIVE_FETCH_TIMEOUT_SEC}s"
        return meta

    gen_path = user_path / "test-profiles" / test_profile / "generated.json"
    meta["ok"] = proc.returncode == 0 and gen_path.is_file()
    meta["returncode"] = proc.returncode
    meta["generated_json"] = str(gen_path) if gen_path.is_file() else None
    if not meta["ok"] and proc.returncode != 0:
        meta["reason"] = f"exit code {proc.returncode}"
    return meta


def _ingest_generated_json_file(
    source: Path,
    test_profile: str,
    *,
    cache_dir: Path | None = None,
    index: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Copy generated.json into ob-cache and merge entries into the in-memory index."""
    cache = Path(cache_dir or default_ob_cache_dir())
    dest = cache / "test-profiles" / test_profile / "generated.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != dest.resolve():
        shutil.copy2(source, dest)
    read_path = dest

    try:
        data = json.loads(read_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    new_entries = _entries_from_generated_json(test_profile, data)
    if index is not None and new_entries:
        merge_entries_into_index(index, new_entries)
    return new_entries


def merge_entries_into_index(index: dict[str, Any], new_entries: dict[str, dict[str, Any]]) -> None:
    entries = index.setdefault("entries", {})
    if not isinstance(entries, dict):
        index["entries"] = {}
        entries = index["entries"]
    entries.update(new_entries)
    index["fallback_buckets"] = _build_fallback_buckets_from_entries(entries)
    index["entry_count"] = len(entries)


def save_ob_cache_index(index: dict[str, Any], index_path: Path | None = None) -> None:
    path = Path(index_path or default_index_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with _index_write_lock:
        path.write_text(json.dumps(index), encoding="utf-8")


def supplement_ob_cache_from_live(
    *,
    dest_dir: Path | None = None,
    profiles: list[str] | None = None,
) -> dict[str, Any]:
    """
    Fetch missing or stale generated.json files from OpenBenchmarking.org.

    Default: latest profile version per test when cache lacks generated.json or file is older
    than BENCHVIZ_OB_CACHE_TTL_HOURS (default 168h / 7 days).
    """
    cache = Path(dest_dir or default_ob_cache_dir())
    profiles_root = cache / "test-profiles"
    meta: dict[str, Any] = {"fetched": 0, "skipped": 0, "failed": 0, "refreshed_stale": 0, "profiles": []}

    if profiles is None:
        profiles = []
        for idx_path in _pts_repo_index_paths():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for test_name, test in (data.get("tests") or {}).items():
                if not isinstance(test, dict):
                    continue
                versions = _sort_profile_versions_desc(test.get("versions") or [])
                if not versions:
                    continue
                qualified = f"pts/{test_name}-{versions[0]}"
                gen = profiles_root / qualified / "generated.json"
                if not gen.is_file() or not _is_cache_fresh(gen):
                    profiles.append(qualified)

    index = load_ob_cache_index() or {
        "entries": {},
        "fallback_buckets": {},
        "entry_count": 0,
    }

    for test_profile in profiles:
        gen_cached = profiles_root / test_profile / "generated.json"
        if gen_cached.is_file() and _is_cache_fresh(gen_cached):
            _ingest_generated_json_file(gen_cached, test_profile, cache_dir=cache, index=index)
            meta["skipped"] += 1
            continue
        was_stale = gen_cached.is_file()
        total = meta["fetched"] + meta["failed"] + meta["skipped"] + 1
        if os.environ.get("BENCHVIZ_OB_SYNC_VERBOSE", "").strip() in ("1", "true", "yes"):
            print(f"OpenBenchmarking live fetch [{total}/{len(profiles)}]: {test_profile}", flush=True)
        fetch_meta = run_pts_fetch_test_profile(test_profile)
        gen_path = fetch_meta.get("generated_json")
        if fetch_meta.get("ok") and gen_path:
            _ingest_generated_json_file(Path(gen_path), test_profile, cache_dir=cache, index=index)
            meta["fetched"] += 1
            if was_stale:
                meta["refreshed_stale"] += 1
            meta["profiles"].append(test_profile)
            continue
        if gen_cached.is_file():
            _ingest_generated_json_file(gen_cached, test_profile, cache_dir=cache, index=index)
            meta["skipped"] += 1
            continue
        meta["failed"] += 1

    if meta["fetched"] or meta["skipped"]:
        index["synced_at"] = datetime.now(timezone.utc).isoformat()
        save_ob_cache_index(index)

    return meta


def _test_profile_version_tuple(test_profile: str | None) -> tuple[int, ...]:
    """Parse trailing -x.y.z from a qualified test profile path."""
    s = (test_profile or "").replace("\\", "/").strip()
    m = re.search(r"-([\d.]+)$", s)
    return parse_version_tuple(m.group(1) if m else "")


def _ingest_cached_profiles_for_identifier(
    index: dict[str, Any],
    identifier: str | None,
    *,
    cache_dir: Path | None = None,
) -> int:
    """Merge all on-disk generated.json for identifier's profile family into the index."""
    cache = Path(cache_dir or default_ob_cache_dir())
    ingested = 0
    for test_profile in list_test_profiles_for_identifier(identifier):
        gen_cached = cache / "test-profiles" / test_profile / "generated.json"
        if gen_cached.is_file():
            _ingest_generated_json_file(gen_cached, test_profile, cache_dir=cache, index=index)
            ingested += 1
    return ingested


def _persist_index_if_updated(index: dict[str, Any], *, updated: bool) -> None:
    if updated:
        index["synced_at"] = datetime.now(timezone.utc).isoformat()
        save_ob_cache_index(index)


def _try_live_ob_lookup(
    comparison_hash: str,
    index: dict[str, Any],
    *,
    identifier: str | None,
) -> tuple[dict[str, Any] | None, ObLookupSource]:
    """
    Acquire generated.json from OB (or on-disk cache) for candidate profile versions.

    Every successful download is mirrored under instance/ob-cache/ and merged into the
    index so later lookups and fallback can use the closest version without re-fetching.
    """
    profiles = list_test_profiles_for_identifier(identifier)
    if not profiles:
        return None, ""

    cache = default_ob_cache_dir()
    index_updated = False

    for test_profile in profiles:
        gen_cached = cache / "test-profiles" / test_profile / "generated.json"
        used_network = False
        gen_path: Path | None = None

        if gen_cached.is_file() and _is_cache_fresh(gen_cached):
            gen_path = gen_cached
        else:
            fetch_meta = run_pts_fetch_test_profile(test_profile)
            remote = fetch_meta.get("generated_json")
            if fetch_meta.get("ok") and remote:
                gen_path = Path(remote)
                used_network = True
            elif gen_cached.is_file():
                gen_path = gen_cached

        if gen_path is None:
            continue

        _ingest_generated_json_file(gen_path, test_profile, cache_dir=cache, index=index)
        index_updated = True

        ent = lookup_ob_entry(comparison_hash, index)
        if ent is not None:
            _persist_index_if_updated(index, updated=True)
            out = dict(ent)
            if used_network:
                out["live_fetched_profile"] = test_profile
            return out, "live" if used_network else "local"

    _persist_index_if_updated(index, updated=index_updated)
    return None, ""


def _try_local_disk_exact(
    comparison_hash: str,
    index: dict[str, Any],
    identifier: str | None,
) -> dict[str, Any] | None:
    """Load exact hash from cached generated.json files not yet in the index."""
    ingested = _ingest_cached_profiles_for_identifier(index, identifier)
    if ingested:
        _persist_index_if_updated(index, updated=True)
    ent = lookup_ob_entry(comparison_hash, index)
    return dict(ent) if ent is not None else None


def _build_fallback_buckets_from_entries(entries: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    for comp_hash, ent in entries.items():
        key = _fallback_bucket_key(
            ent.get("test_profile") or "",
            ent.get("description") or "",
            ent.get("unit") or "",
        )
        buckets.setdefault(key, []).append(comp_hash)
    return buckets


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
                entries[str(comp_hash)] = _entry_from_overview_row(test_profile, str(comp_hash), row)

    payload = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "cache_dir": str(cache),
        "pts_clone_dir": str(default_pts_clone_dir()),
        "files_read": files_read,
        "entry_count": len(entries),
        "entries": entries,
        "fallback_buckets": _build_fallback_buckets_from_entries(entries),
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


def _fallback_bucket_key(test_profile: str, description: str, unit: str) -> str:
    return f"{test_profile_family(test_profile)}\0{(description or '').strip()}\0{normalize_ob_unit(unit)}"


def ensure_fallback_buckets(index: dict[str, Any]) -> dict[str, list[str]]:
    """Build or return family+description+unit → [comparison_hash] buckets."""
    cached = index.get("fallback_buckets")
    if isinstance(cached, dict):
        return cached

    buckets: dict[str, list[str]] = {}
    for comp_hash, ent in (index.get("entries") or {}).items():
        if not isinstance(ent, dict):
            continue
        key = _fallback_bucket_key(
            ent.get("test_profile") or "",
            ent.get("description") or "",
            ent.get("unit") or "",
        )
        buckets.setdefault(key, []).append(str(comp_hash))

    index["fallback_buckets"] = buckets
    return buckets


def _verify_fallback_entry_match(
    entry: dict[str, Any],
    stored_hash: str,
    *,
    arguments: str,
    description: str,
    scale: str,
) -> bool:
    """True when entry matches options; hash is checked using the source profile id."""
    if (entry.get("description") or "").strip() != (description or "").strip():
        return False
    req_unit = normalize_ob_unit(scale)
    ent_unit = normalize_ob_unit(entry.get("unit") or "")
    if req_unit and ent_unit and req_unit != ent_unit:
        return False
    test_identifier = hash_identifier_from_test_profile(entry.get("test_profile") or "")
    if not test_identifier:
        return False
    return generate_comparison_hash(
        test_identifier,
        arguments,
        description,
        entry.get("app_version") or "",
        entry.get("unit") or scale or "",
    ) == stored_hash


def _collect_version_fallback_candidates(
    index: dict[str, Any],
    *,
    identifier: str | None,
    arguments: str,
    description: str,
    scale: str,
) -> tuple[list[tuple[dict[str, Any], str]], int]:
    """All index entries in this profile family with matching description, unit, and args."""
    tp = strip_test_profile_identifier(identifier)
    family = test_profile_family(tp) if tp else ""
    if not family or not (description or "").strip():
        return [], 0

    ingested = _ingest_cached_profiles_for_identifier(index, identifier)
    ensure_fallback_buckets(index)

    desc = (description or "").strip()
    req_unit = normalize_ob_unit(scale)
    candidates: list[tuple[dict[str, Any], str]] = []
    entries = index.get("entries") or {}

    bucket_key = _fallback_bucket_key(family, desc, scale)
    for stored_hash in (index.get("fallback_buckets") or {}).get(bucket_key) or []:
        row = entries.get(stored_hash)
        if isinstance(row, dict) and _verify_fallback_entry_match(
            row, stored_hash, arguments=arguments or "", description=desc, scale=scale
        ):
            candidates.append((row, stored_hash))

    if not candidates:
        for stored_hash, row in entries.items():
            if not isinstance(row, dict):
                continue
            if test_profile_family(row.get("test_profile") or "") != family:
                continue
            if (row.get("description") or "").strip() != desc:
                continue
            if req_unit:
                ent_unit = normalize_ob_unit(row.get("unit") or "")
                if ent_unit and ent_unit != req_unit:
                    continue
            if _verify_fallback_entry_match(
                row, stored_hash, arguments=arguments or "", description=desc, scale=scale
            ):
                candidates.append((row, stored_hash))

    return candidates, ingested


def _pick_version_fallback_entry(
    candidates: list[tuple[dict[str, Any], str]],
    requested_version: str,
) -> tuple[dict[str, Any], str] | None:
    if not candidates:
        return None

    def app_version_key(ent: dict[str, Any]) -> tuple[int, ...]:
        return parse_version_tuple(ent.get("app_version"))

    requested = parse_version_tuple(requested_version)

    def candidate_rank(c: tuple[dict[str, Any], str]) -> tuple[Any, ...]:
        ent = c[0]
        profile_ver = _test_profile_version_tuple(ent.get("test_profile"))
        app_ver = app_version_key(ent)
        app_ok = (not requested) or (app_ver <= requested)
        # Prefer newest cached test profile, then closest app_version at or below request.
        return (
            profile_ver,
            app_ver if app_ok else (),
            int(ent.get("samples") or 0),
        )

    return max(candidates, key=candidate_rank)


def lookup_ob_entry_with_fallback(
    comparison_hash: str,
    index: dict[str, Any] | None,
    *,
    identifier: str | None = None,
    title: str | None = None,
    arguments: str = "",
    description: str = "",
    app_version: str = "",
    scale: str = "",
    allow_live: bool = False,
) -> tuple[dict[str, Any] | None, ObLookupSource]:
    """
    Resolve OB population data in priority order:

    1. Local index / ob-cache (exact hash)
    2. Older profile version with matching options (fallback)
    3. Live fetch from OpenBenchmarking.org (only when allow_live=True; never during compare by default)
    """
    from .pts_comparison import comparison_hash_for_benchmark

    idx = index if index is not None else load_ob_cache_index()
    if idx is None:
        idx = {"entries": {}, "fallback_buckets": {}}

    desc = (description or "").strip()
    unit = (scale or "").strip()

    ent = lookup_ob_entry(comparison_hash, idx)
    if ent is not None and (_index_entry_cache_fresh(ent) or not allow_live):
        return ent, "local"

    disk_ent = _try_local_disk_exact(comparison_hash, idx, identifier)
    if disk_ent is not None:
        return disk_ent, "local"

    tp = strip_test_profile_identifier(identifier)
    if not tp:
        tp = (title or "").strip()
    if not tp or not desc:
        if ent is not None:
            return ent, "local"
        return None, ""

    candidates, ingested = _collect_version_fallback_candidates(
        idx,
        identifier=identifier,
        arguments=arguments or "",
        description=desc,
        scale=unit,
    )
    if ingested:
        _persist_index_if_updated(idx, updated=True)

    picked = _pick_version_fallback_entry(candidates, app_version or "")
    if picked is not None:
        entry, _ = picked
        out = dict(entry)
        out["fallback"] = True
        out["requested_app_version"] = (app_version or "").strip()
        out["comparison_hash"] = comparison_hash_for_benchmark(
            identifier=identifier,
            title=title,
            description=desc,
            app_version=app_version,
            scale=unit,
            arguments=arguments or "",
        )
        return out, "fallback"

    if allow_live and live_ob_fetch_enabled():
        live_ent, live_source = _try_live_ob_lookup(comparison_hash, idx, identifier=identifier)
        if live_ent is not None:
            return live_ent, live_source

    if ent is not None:
        return ent, "local"
    return None, ""


def lookup_ob_entry(comparison_hash: str, index: dict[str, Any] | None = None) -> dict[str, Any] | None:
    idx = index if index is not None else load_ob_cache_index()
    if not idx:
        return None
    ent = (idx.get("entries") or {}).get(comparison_hash)
    return ent if isinstance(ent, dict) else None
