"""Phoronix Test Suite math aggregation — harmonic/geometric mean composites."""

from __future__ import annotations

import math
from typing import Any

from ..pts_math import geometric_mean, harmonic_mean
from .ob_baselines import lib_to_hib_value


MIN_HARMONIC_SUBTESTS = 4


def is_harmonic_mean_scale(scale: str | None) -> bool:
    return bool((scale or "").strip())


def normalize_harmonic_scale_key(scale: str | None) -> str | None:
    rs = (scale or "").strip()
    if not rs:
        return None
    rs_lower = rs.lower()
    if rs_lower in ("mb/s", "mib/s"):
        return "MB/s"
    if "byte" in rs_lower and ("/" in rs or "sec" in rs_lower or " per " in rs_lower):
        return "MB/s"
    if rs_lower.endswith("/s") and ("mib" in rs_lower or "mb" in rs_lower):
        return "MB/s"
    if "fps" in rs_lower or ("frame" in rs_lower and "second" in rs_lower):
        return "FPS"
    if rs_lower == "mips" or "mips" in rs_lower or "million instructions" in rs_lower:
        return "MIPS"
    if "iops" in rs_lower:
        return "IOPS"
    if "bps" in rs_lower:
        return "bps"
    if "run" in rs_lower and ("/" in rs or " per " in rs_lower):
        return "runs/min"
    return rs


def _reference_system_from_raw(
    raw: dict[str, float | None],
    system_ids: list[str],
) -> str:
    ref_id = system_ids[0] if system_ids else ""
    min_v = None
    for sid in system_ids:
        c = raw.get(sid)
        if c is None:
            continue
        if min_v is None or c < min_v:
            min_v = c
            ref_id = sid
    return ref_id


def pts_harmonic_mean_by_scale(
    subtests: list[dict[str, Any]],
    system_ids: list[str],
) -> dict[str, dict[str, Any]]:
    by_scale: dict[str, dict[str, list[float]]] = {}
    subtest_counts: dict[str, int] = {}

    for st in subtests:
        if st.get("hib") is False:
            continue
        scale = normalize_harmonic_scale_key(st.get("scale"))
        if not scale:
            continue
        p1_rel = st.get("pts_ob_p1_relative") or {}
        ob = st.get("ob") or {}
        if not p1_rel and not ob.get("p1"):
            continue
        bucket = by_scale.setdefault(scale, {sid: [] for sid in system_ids})
        contributed = False
        for sid in system_ids:
            raw = p1_rel.get(sid)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v <= 0 or not math.isfinite(v):
                continue
            bucket[sid].append(v)
            contributed = True
        if contributed:
            subtest_counts[scale] = subtest_counts.get(scale, 0) + 1

    out: dict[str, dict[str, Any]] = {}
    for scale, per_system in by_scale.items():
        raw: dict[str, float | None] = {}
        for sid in system_ids:
            arr = per_system.get(sid) or []
            raw[sid] = harmonic_mean(arr) if len(arr) >= MIN_HARMONIC_SUBTESTS else None

        valid_systems = [sid for sid in system_ids if raw.get(sid) is not None]
        if len(valid_systems) < 2:
            continue

        out[scale] = {
            "raw": raw,
            "relative": {sid: raw.get(sid) for sid in system_ids},
            "reference_system_id": "",
            "subtest_count": subtest_counts.get(scale, 0),
            "ob_p1_baseline": True,
        }
    return out


def pts_harmonic_mean_cross_scale(
    subtests: list[dict[str, Any]],
    system_ids: list[str],
    *,
    head_to_head: bool = False,
) -> dict[str, Any] | None:
    del head_to_head
    per_system: dict[str, list[float]] = {sid: [] for sid in system_ids}
    subtest_count = 0

    for st in subtests:
        if st.get("hib") is False:
            continue
        p1_rel = st.get("pts_ob_p1_relative") or {}
        ob = st.get("ob") or {}
        if not p1_rel and not ob.get("p1"):
            continue
        contributed = False
        for sid in system_ids:
            raw = p1_rel.get(sid)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v <= 0 or not math.isfinite(v):
                continue
            per_system[sid].append(v)
            contributed = True
        if contributed:
            subtest_count += 1

    if subtest_count < MIN_HARMONIC_SUBTESTS:
        return None

    raw: dict[str, float | None] = {}
    for sid in system_ids:
        arr = per_system.get(sid) or []
        raw[sid] = harmonic_mean(arr) if len(arr) >= MIN_HARMONIC_SUBTESTS else None

    valid_systems = [sid for sid in system_ids if raw.get(sid) is not None]
    if len(valid_systems) < 2:
        return None

    return {
        "raw": raw,
        "relative": {sid: raw.get(sid) for sid in system_ids},
        "reference_system_id": "",
        "ob_baseline": True,
        "ob_p1_baseline": True,
        "subtest_count": subtest_count,
    }


def pts_geometric_mean_composite(
    subtest_values: list[dict[str, float | None]],
    system_ids: list[str],
    hib_flags: list[bool],
) -> dict[str, float | None]:
    per_system: dict[str, list[float]] = {sid: [] for sid in system_ids}
    for vals, hib in zip(subtest_values, hib_flags):
        for sid in system_ids:
            raw = vals.get(sid)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v <= 0 or not math.isfinite(v):
                continue
            per_system[sid].append(lib_to_hib_value(v) if not hib else v)

    out: dict[str, float | None] = {}
    for sid in system_ids:
        arr = per_system[sid]
        out[sid] = geometric_mean(arr) if len(arr) >= 2 else None
    return out


def pts_geometric_mean_ob_composite(
    subtests: list[dict[str, Any]],
    system_ids: list[str],
) -> dict[str, float | None] | None:
    per_system: dict[str, list[float]] = {sid: [] for sid in system_ids}
    subtest_count = 0

    for st in subtests:
        if not (st.get("ob") or {}).get("matched"):
            continue
        ob_rel = st.get("pts_ob_relative") or {}
        parsed: dict[str, float] = {}
        for sid in system_ids:
            raw = ob_rel.get(sid)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v <= 0 or not math.isfinite(v):
                continue
            parsed[sid] = v
        if len(parsed) < len(system_ids):
            continue
        for sid, v in parsed.items():
            per_system[sid].append(v)
        subtest_count += 1

    if subtest_count < 2:
        return None

    return {
        sid: geometric_mean(arr) if len(arr) >= 2 else None
        for sid, arr in per_system.items()
    }
