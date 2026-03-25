"""
Sync `hardware_theoretical_ranks` from a local Parts API (GET /api/cpu, GET /api/gpu).

Rank formulas are heuristic composites of published specs (higher = theoretically stronger).
Match keys use the same normalization as BenchViz cohort strings (`hardware_rank_match_key`).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from app import db
from app.components import clean_text, hardware_rank_match_key
from app.models import HardwareTheoreticalRank


def _parse_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    # e.g. "5.7 GHz" or "1,792"
    s = s.replace(",", "")
    m = re.search(r"[-+]?(?:\d*\.?\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?", s)
    if not m:
        return default
    try:
        return float(m.group(0))
    except ValueError:
        return default


def _fetch_json_list(base_url: str, path: str, timeout: int) -> list[dict[str, Any]]:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — trusted local service URL
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "data", "results", "cpus", "gpus", "records"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _cpu_label_from_api(row: dict[str, Any]) -> str:
    m = clean_text(row.get("Manufacturer"))
    s = clean_text(row.get("Series"))
    model = clean_text(row.get("Model"))
    return " ".join(p for p in (m, s, model) if p)


def _gpu_label_from_api(row: dict[str, Any]) -> str:
    b = clean_text(row.get("Brand"))
    model = clean_text(row.get("Model"))
    return " ".join(p for p in (b, model) if p)


def cpu_rank_value_from_api(row: dict[str, Any]) -> tuple[float, str]:
    """
    Composite: cores × max(boost, base GHz) × min(threads/cores, 2).
    SMT/HT capped so dual-thread-per-core doesn't dominate.
    """
    cores = _parse_float(row.get("Cores"), 0.0)
    threads = _parse_float(row.get("Threads"), cores)
    base_ghz = _parse_float(row.get("Base Clock"), 0.0)
    boost_ghz = _parse_float(row.get("Max Boost"), 0.0)
    if cores <= 0:
        return 0.0, "skip"
    thread_ratio = min(threads / max(cores, 1e-9), 2.0)
    ghz = max(boost_ghz, base_ghz)
    if ghz <= 0:
        score = cores * thread_ratio
        note = f"api: cores×min(τ/c,2) (no clock)={cores:.0f}×{thread_ratio:g}"
        return score, note
    score = cores * ghz * thread_ratio
    note = f"api: cores×max(boost,base)×min(τ/c,2)={cores:.0f}×{ghz:g}×{thread_ratio:g}"
    return score, note


def gpu_rank_value_from_api(row: dict[str, Any]) -> tuple[float, str]:
    """
    Composite: TDP (W) × frame-buffer bandwidth (GB/s) when both make sense;
    if TDP is missing, fall back to bandwidth alone for ordering.
    """
    tdp = _parse_float(row.get("TDP (W)"), 0.0)
    bw = _parse_float(row.get("Frame Buffer Bandwidth (GB/s)"), 0.0)
    if tdp > 0:
        score = tdp * max(bw, 1.0)
        note = f"api: TDP×max(BW,1)={tdp:g}×{max(bw, 1.0):g}"
    elif bw > 0:
        score = bw
        note = f"api: BW only={bw:g}"
    else:
        return 0.0, "skip"
    return score, note


def _merge_best_by_match_key(
    entries: list[tuple[str, float, str, str, str]],
) -> list[tuple[str, float, str, str, str]]:
    """If the API returns duplicates, keep the highest rank_value per match_key."""
    best: dict[str, tuple[str, float, str, str, str]] = {}
    for kind, mk, rv, label, note in entries:
        if not mk or rv <= 0:
            continue
        cur = best.get(mk)
        # Tuple is (kind, match_key, rank_value, label, note) — rank is index 2, not 1.
        if cur is None or rv > cur[2]:
            best[mk] = (kind, mk, rv, label, note)
    return list(best.values())


def build_rank_entries_from_api(
    base_url: str,
    timeout: int = 120,
) -> tuple[list[tuple[str, float, str, str, str]], list[str]]:
    """
    Returns (entries, errors) where each entry is
    (part_kind, match_key, rank_value, display_label, source_note).
    """
    errors: list[str] = []
    raw: list[tuple[str, float, str, str, str]] = []

    try:
        cpu_rows = _fetch_json_list(base_url, "/api/cpu", timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        errors.append(f"GET /api/cpu failed: {e}")
        cpu_rows = []

    try:
        gpu_rows = _fetch_json_list(base_url, "/api/gpu", timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        errors.append(f"GET /api/gpu failed: {e}")
        gpu_rows = []

    skipped_cpu = 0
    for row in cpu_rows:
        label = _cpu_label_from_api(row)
        if not label:
            skipped_cpu += 1
            continue
        mk = hardware_rank_match_key("processor", label)
        if not mk:
            skipped_cpu += 1
            continue
        rv, detail = cpu_rank_value_from_api(row)
        if detail == "skip" or rv <= 0:
            skipped_cpu += 1
            continue
        tdp = _parse_float(row.get("TDP (Watts)"), 0.0)
        note = f"parts API CPU; {detail}"
        if tdp > 0:
            note += f"; TDP={tdp:g}W"
        if len(note) > 255:
            note = note[:252] + "..."
        raw.append(("cpu", mk, rv, label[:512], note))

    skipped_gpu = 0
    for row in gpu_rows:
        label = _gpu_label_from_api(row)
        if not label:
            skipped_gpu += 1
            continue
        mk = hardware_rank_match_key("graphics", label)
        if not mk:
            skipped_gpu += 1
            continue
        rv, detail = gpu_rank_value_from_api(row)
        if rv <= 0:
            skipped_gpu += 1
            continue
        note = f"parts API GPU; {detail}"
        if len(note) > 255:
            note = note[:252] + "..."
        raw.append(("gpu", mk, rv, label[:512], note))

    if skipped_cpu:
        errors.append(f"Skipped {skipped_cpu} CPU row(s) (missing name or cores/clock).")
    if skipped_gpu:
        errors.append(f"Skipped {skipped_gpu} GPU row(s) (missing name or TDP×BW).")

    merged = _merge_best_by_match_key(raw)
    return merged, errors


def upsert_theoretical_ranks(
    entries: list[tuple[str, float, str, str, str]],
) -> dict[str, int]:
    """Apply entries to SQLAlchemy session (caller commits)."""
    counters = {"added": 0, "updated": 0}
    for kind, mk, rv, label, note in entries:
        rec = HardwareTheoreticalRank.query.filter_by(part_kind=kind, match_key=mk).first()
        if rec:
            rec.rank_value = rv
            rec.display_label = label or None
            rec.source_note = note or None
            counters["updated"] += 1
        else:
            db.session.add(HardwareTheoreticalRank(
                part_kind=kind,
                match_key=mk,
                rank_value=rv,
                display_label=label or None,
                source_note=note or None,
            ))
            counters["added"] += 1
    return counters
