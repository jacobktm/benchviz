from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.ob_cache_sync._data import (
    _fallback_bucket_key,
    _ingest_generated_json_file,
    _pts_repo_index_paths,
    _sort_profile_versions_desc,
    _test_profile_version_tuple,
    ensure_fallback_buckets,
    list_test_profiles_for_identifier,
    load_ob_cache_index,
    merge_entries_into_index,
    save_ob_cache_index,
)
from app.ob_cache_sync._paths import (
    LIVE_FETCH_TIMEOUT_SEC,
    ObLookupSource,
    _cached_generated_json_path,
    _is_cache_fresh,
    default_ob_cache_dir,
    default_pts_clone_dir,
    default_pts_user_path,
    live_ob_fetch_enabled,
    pts_executable,
    pts_user_path_override_value,
)
from app.pts.hashing import (
    comparison_hash_for_benchmark,
    generate_comparison_hash,
    hash_identifier_from_test_profile,
    normalize_ob_unit,
    parse_version_tuple,
    strip_test_profile_identifier,
    test_profile_family,
)


# ── Fetch test profile via PTS ────────────────────────────────────

def run_pts_fetch_test_profile(test_profile: str) -> dict[str, Any]:
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


# ── Supplement (live fetch for stale/missing profiles) ─────────────

def supplement_ob_cache_from_live(
    *,
    dest_dir: Path | None = None,
    profiles: list[str] | None = None,
) -> dict[str, Any]:
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


# ── Ingest / persist helpers ──────────────────────────────────────

def _ingest_cached_profiles_for_identifier(
    index: dict[str, Any],
    identifier: str | None,
    *,
    cache_dir: Path | None = None,
) -> int:
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


# ── Try live OB lookup ────────────────────────────────────────────

def _try_live_ob_lookup(
    comparison_hash: str,
    index: dict[str, Any],
    *,
    identifier: str | None,
) -> tuple[dict[str, Any] | None, ObLookupSource]:
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


# ── Local disk exact lookup ───────────────────────────────────────

def _try_local_disk_exact(
    comparison_hash: str,
    index: dict[str, Any],
    identifier: str | None,
) -> dict[str, Any] | None:
    ingested = _ingest_cached_profiles_for_identifier(index, identifier)
    if ingested:
        _persist_index_if_updated(index, updated=True)
    ent = lookup_ob_entry(comparison_hash, index)
    return dict(ent) if ent is not None else None


# ── Fallback entry verification ──────────────────────────────────

def _verify_fallback_entry_match(
    entry: dict[str, Any],
    stored_hash: str,
    *,
    arguments: str,
    description: str,
    scale: str,
) -> bool:
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
        return (
            profile_ver,
            app_ver if app_ok else (),
            int(ent.get("samples") or 0),
        )

    return max(candidates, key=candidate_rank)


# ── Public lookup API ─────────────────────────────────────────────

def lookup_ob_entry(comparison_hash: str, index: dict[str, Any] | None = None) -> dict[str, Any] | None:
    idx = index if index is not None else load_ob_cache_index()
    if not idx:
        return None
    ent = (idx.get("entries") or {}).get(comparison_hash)
    return ent if isinstance(ent, dict) else None


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
    idx = index if index is not None else load_ob_cache_index()
    if idx is None:
        idx = {"entries": {}, "fallback_buckets": {}}

    desc = (description or "").strip()
    unit = (scale or "").strip()

    ent = lookup_ob_entry(comparison_hash, idx)
    if ent is not None and (_is_cache_fresh(ent) or not allow_live):
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
