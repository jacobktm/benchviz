"""Phoronix Test Suite comparison hashing and relative scoring."""

from __future__ import annotations

import hashlib
import math
from typing import Any

from .pts_math import geometric_mean, result_to_percentile

# Cap matches BenchViz composite cap (PTS itself does not cap geo-mean inputs).
COMPOSITE_OPTION_CAP_RATIO = 1.5


def strip_test_profile_identifier(identifier: str | None) -> str:
    """
    Match pts_test_result::get_comparison_hash() identifier trimming.

    Removes the last dotted segment (xx.yy.zz → xx.yy) when present.
    """
    tp = (identifier or "").strip()
    if not tp:
        return ""
    dot = tp.rfind(".")
    if dot != -1:
        tp = tp[:dot]
    return tp


def generate_comparison_hash(
    test_identifier: str,
    arguments: str = "",
    attributes: str = "",
    version: str = "",
    result_scale: str = "",
    *,
    hex_digest: bool = True,
) -> str:
    """SHA1 comparison hash used by OpenBenchmarking.org (pts_test_profile::generate_comparison_hash)."""
    parts = [
        test_identifier or "",
        (arguments or "").strip(),
        (attributes or "").strip(),
        (version or "").strip(),
        (result_scale or "").strip(),
    ]
    payload = ",".join(parts)
    digest = hashlib.sha1(payload.encode("utf-8")).digest()
    return digest.hex() if hex_digest else digest


def comparison_hash_for_benchmark(
    *,
    identifier: str | None,
    title: str | None,
    description: str | None,
    app_version: str | None,
    scale: str | None,
    arguments: str = "",
) -> str:
    """Build OB comparison hash from a BenchViz Benchmark + result arguments."""
    tp = strip_test_profile_identifier(identifier)
    if not tp:
        tp = (title or "").strip()
    return generate_comparison_hash(
        tp,
        arguments or "",
        description or "",
        app_version or "",
        scale or "",
    )


def lib_to_hib_value(value: float) -> float:
    """PTS geo-mean path: LIB results become (1/r)*100 before blending."""
    return (1.0 / float(value)) * 100.0


def normalize_relative_values(
    values_by_system: dict[str, float | None],
    *,
    hib: bool,
    reference_system: str | None = None,
) -> dict[str, float | None]:
    """
    PTS normalize_buffer_values() for a single BAR_GRAPH subtest.

    LIB inverted first; divide by reference (explicit) or best in set (default).
    Returns relative performance multipliers (best/reference = 1.0).
    """
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


def capped_relative_score(value: float | None, reference: float | None, hib: bool) -> float | None:
    """Relative score vs reference with BenchViz cap (PTS normalize has no cap)."""
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


def pts_geometric_mean_composite(
    subtest_values: list[dict[str, float | None]],
    system_ids: list[str],
    hib_flags: list[bool],
) -> dict[str, float | None]:
    """
    PTS generate_geometric_mean_result() across subtests for one benchmark group.

    Each subtest dict maps system_id -> raw BAR_GRAPH value. LIB tests inverted to HIB scale.
    """
    import math

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
        out[sid] = geometric_mean(arr) if len(arr) >= 1 else None
    return out


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
