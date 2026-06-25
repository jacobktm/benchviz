from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.ob_cache_sync._paths import (
    default_ob_cache_dir,
    default_index_path,
    default_pts_clone_dir,
    default_pts_user_path,
)
from app.pts.hashing import (
    generate_comparison_hash,
    hash_identifier_from_test_profile,
    normalize_ob_unit,
    parse_version_tuple,
    strip_test_profile_identifier,
    test_profile_family,
)

_index_write_lock = threading.Lock()
_index_cache: dict[str, Any] | None = None
_index_cache_lock = threading.Lock()


# ── Entry parsing from generated.json ──────────────────────────────

def _ob_median_from_percentiles(percentiles: list[Any]) -> float | None:
    return _ob_percentile_from_list(percentiles, 50)


def _ob_p1_from_percentiles(percentiles: list[Any]) -> float | None:
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


# ── Version helpers ────────────────────────────────────────────────

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


def _test_profile_version_tuple(test_profile: str | None) -> tuple[int, ...]:
    s = (test_profile or "").replace("\\", "/").strip()
    m = re.search(r"-([\d.]+)$", s)
    return parse_version_tuple(m.group(1) if m else "")


# ── PTS repo index paths ──────────────────────────────────────────

def _pts_repo_index_paths() -> list[Path]:
    paths: list[Path] = []
    user_idx = default_pts_user_path() / "openbenchmarking.org" / "pts.index"
    if user_idx.is_file():
        paths.append(user_idx)
    clone_idx = default_pts_clone_dir() / "ob-cache" / "openbenchmarking.org" / "pts.index"
    if clone_idx.is_file():
        paths.append(clone_idx)
    return paths


# ── Test profile listing ──────────────────────────────────────────

def list_disk_test_profiles_for_family(family: str, cache_dir: Path | None = None) -> list[str]:
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


# ── Ingest generated.json into cache + index ──────────────────────

def _ingest_generated_json_file(
    source: Path,
    test_profile: str,
    *,
    cache_dir: Path | None = None,
    index: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
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


# ── Index save / load ─────────────────────────────────────────────

def _fallback_bucket_key(test_profile: str, description: str, unit: str) -> str:
    return f"{test_profile_family(test_profile)}\0{(description or '').strip()}\0{normalize_ob_unit(unit)}"


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
    _invalidate_index_cache()


def build_ob_cache_index(cache_dir: Path | None = None, index_path: Path | None = None) -> dict[str, Any]:
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


def _try_track_cache(metric_name: str) -> None:
    try:
        from flask import current_app
        metrics = current_app.extensions.get("benchviz_metrics")
        if metrics is not None:
            getattr(metrics, metric_name)()
    except Exception:
        pass


def _track_ob_cache_hit() -> None:
    _try_track_cache("ob_cache.hit")


def _track_ob_cache_miss() -> None:
    _try_track_cache("ob_cache.miss")


def _invalidate_index_cache() -> None:
    global _index_cache
    with _index_cache_lock:
        _index_cache = None


def load_ob_cache_index(index_path: Path | None = None) -> dict[str, Any] | None:
    global _index_cache
    path = Path(index_path or default_index_path())

    with _index_cache_lock:
        if _index_cache is not None:
            _track_ob_cache_hit()
            return _index_cache

    _track_ob_cache_miss()

    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    result = data if isinstance(data, dict) else None

    with _index_cache_lock:
        _index_cache = result
    return result


def ensure_fallback_buckets(index: dict[str, Any]) -> dict[str, list[str]]:
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
