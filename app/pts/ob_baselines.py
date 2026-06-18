"""OpenBenchmarking.org baseline lookups and relative performance scoring."""

from __future__ import annotations

import math
from typing import Any

from ..pts_math import result_to_percentile
from .hashing import COMPOSITE_OPTION_CAP_RATIO


def lib_to_hib_value(value: float) -> float:
    return (1.0 / float(value)) * 100.0


def ob_median_from_entry(ob_entry: dict[str, Any] | None) -> float | None:
    return ob_percentile_value_from_entry(ob_entry, 50)


def ob_p1_from_entry(ob_entry: dict[str, Any] | None) -> float | None:
    return ob_percentile_value_from_entry(ob_entry, 0)


def ob_percentile_value_from_entry(
    ob_entry: dict[str, Any] | None,
    percentile_index: int,
) -> float | None:
    if not ob_entry or percentile_index < 0:
        return None
    cache_key = f"ob_p{percentile_index}" if percentile_index != 50 else "ob_median"
    if percentile_index == 0:
        cache_key = "ob_p1"
    cached = ob_entry.get(cache_key)
    if cached is not None:
        try:
            v = float(cached)
            return v if v > 0 and math.isfinite(v) else None
        except (TypeError, ValueError):
            pass
    percentiles = ob_entry.get("percentiles") or []
    if len(percentiles) <= percentile_index:
        return None
    try:
        v = float(percentiles[percentile_index])
    except (TypeError, ValueError):
        return None
    return v if v > 0 and math.isfinite(v) else None


def relative_vs_ob_baseline(
    values_by_system: dict[str, float | None],
    *,
    hib: bool,
    baseline: float | None,
) -> dict[str, float | None]:
    if baseline is None or baseline <= 0:
        return {k: None for k in values_by_system}
    ref = float(baseline)
    out: dict[str, float | None] = {}
    for sys_id, raw in values_by_system.items():
        if raw is None:
            out[sys_id] = None
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            out[sys_id] = None
            continue
        if v <= 0 or not math.isfinite(v):
            out[sys_id] = None
            continue
        out[sys_id] = round(v / ref, 6) if hib else round(ref / v, 6)
    return out


def relative_vs_ob_median(
    values_by_system: dict[str, float | None],
    *,
    hib: bool,
    ob_median: float | None,
) -> dict[str, float | None]:
    return relative_vs_ob_baseline(values_by_system, hib=hib, baseline=ob_median)


def normalize_relative_values(
    values_by_system: dict[str, float | None],
    *,
    hib: bool,
    reference_system: str | None = None,
) -> dict[str, float | None]:
    working: dict[str, float] = {}
    for sys_id, raw in values_by_system.items():
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v <= 0 or not math.isfinite(v):
            continue
        working[sys_id] = v if hib else (1.0 / v)

    if not working:
        return {k: None for k in values_by_system}

    divide = None
    if reference_system and reference_system in working:
        divide = working[reference_system]
    if divide is None or divide <= 0:
        divide = min(working.values())

    out: dict[str, float | None] = {}
    for sys_id in values_by_system:
        v = working.get(sys_id)
        if v is None or divide is None or divide <= 0:
            out[sys_id] = None
            continue
        out[sys_id] = round(v / divide, 6)
    return out


def capped_relative_score(
    value: float | None, reference: float | None, hib: bool
) -> float | None:
    if value is None or reference is None:
        return None
    try:
        v = float(value)
        ref = float(reference)
    except (TypeError, ValueError):
        return None
    if v <= 0 or ref <= 0:
        return None
    if hib:
        score = v / ref
    else:
        score = ref / v
    if score > COMPOSITE_OPTION_CAP_RATIO:
        score = COMPOSITE_OPTION_CAP_RATIO
    return score


def ob_percentiles_for_systems(
    values_by_system: dict[str, float | None],
    ob_entry: dict[str, Any] | None,
) -> dict[str, int | None]:
    if not ob_entry:
        return {k: None for k in values_by_system}
    percentiles = ob_entry.get("percentiles") or []
    hib = bool(ob_entry.get("hib", 1))
    return {
        sid: result_to_percentile(v, percentiles, hib) if v is not None else None
        for sid, v in values_by_system.items()
    }
