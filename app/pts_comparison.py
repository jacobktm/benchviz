"""Phoronix Test Suite comparison hashing and relative scoring."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from .pts_math import geometric_mean, harmonic_mean, result_to_percentile

# Cap matches BenchViz composite cap (PTS itself does not cap geo-mean inputs).
COMPOSITE_OPTION_CAP_RATIO = 1.5


def strip_test_profile_identifier(identifier: str | None) -> str:
    """
    Match pts_test_result::get_comparison_hash() identifier trimming.

    Removes the patch segment from xx.yy.zz profile ids (e.g. 1.17.1 → 1.17).
    Two-part ids such as build-linux-kernel-1.17 are left unchanged.
    """
    tp = (identifier or "").strip().replace("\\", "/")
    if not tp:
        return ""
    m = re.search(r"-(\d+)\.(\d+)\.(\d+)$", tp)
    if m:
        tp = tp[: m.start()] + f"-{m.group(1)}.{m.group(2)}"
    return tp


def hash_identifier_from_test_profile(test_profile: str | None) -> str:
    """Comparison-hash test_identifier for a mirrored ob-cache profile directory."""
    return strip_test_profile_identifier(test_profile)


def test_profile_family(name: str | None) -> str:
    """
    Benchmark family key for OB fallback (strip trailing -x.y profile version).

    pts/compress-7zip-1.10.0 → pts/compress-7zip
    """
    s = (name or "").strip().replace("\\", "/")
    if not s:
        return ""
    return re.sub(r"-\d[\d.]*$", "", s)


def normalize_ob_unit(unit: str | None) -> str:
    """Canonical OB unit string for fallback bucket matching (Seconds/sec/s → seconds)."""
    u = (unit or "").strip()
    if not u:
        return ""
    ul = u.lower()
    if ul in ("seconds", "second", "sec", "s"):
        return "seconds"
    if ul in ("ms", "millisecond", "milliseconds"):
        return "ms"
    if ul in ("mb/s", "mib/s"):
        return "mb/s"
    if ul in ("gb/s", "gib/s"):
        return "gb/s"
    if ul in ("fps", "frames per second", "frame/s"):
        return "fps"
    if ul in ("mips",):
        return "mips"
    if ul in ("iops",):
        return "iops"
    return ul


def parse_version_tuple(version: str | None) -> tuple[int, ...]:
    """Numeric prefix of an app/test version string for ordering (22.01 → (22, 1))."""
    version = (version or "").strip()
    if not version:
        return ()
    parts: list[int] = []
    for piece in re.split(r"[._-]", version):
        if piece.isdigit():
            parts.append(int(piece))
        elif piece:
            break
    return tuple(parts)


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


def ob_median_from_entry(ob_entry: dict[str, Any] | None) -> float | None:
    """Population median from an OB cache entry (percentiles[50])."""
    return ob_percentile_value_from_entry(ob_entry, 50)


def ob_p1_from_entry(ob_entry: dict[str, Any] | None) -> float | None:
    """Population baseline from an OB cache entry (percentiles[0] from generated.json)."""
    return ob_percentile_value_from_entry(ob_entry, 0)


def ob_percentile_value_from_entry(
    ob_entry: dict[str, Any] | None,
    percentile_index: int,
) -> float | None:
    """Value at a given OB population percentile rank (same indexing as percentiles[50] median)."""
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
    """
    Relative performance vs an OpenBenchmarking population reference value.

    Returns multipliers where 1.0 = OB reference result for that subtest.
    """
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
    """
    Relative performance vs OpenBenchmarking population median.

    Returns multipliers where 1.0 = OB median result (PTS box-plot baseline).
    """
    return relative_vs_ob_baseline(values_by_system, hib=hib, baseline=ob_median)


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


# PTS generate_harmonic_mean_result() requires ≥4 eligible subtests per system.
MIN_HARMONIC_SUBTESTS = 4


def is_harmonic_mean_scale(scale: str | None) -> bool:
    """True when scale is non-empty (any HIB unit can form a harmonic bucket)."""
    return bool((scale or "").strip())


def normalize_harmonic_scale_key(scale: str | None) -> str | None:
    """Canonical scale bucket for cross-benchmark harmonic mean (any HIB unit)."""
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
    """
    Harmonic mean grouped by result scale.

    Uses per-subtest OB baseline-relative HIB scores (1.0 = percentiles[0]).
    """
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
    """
    Harmonic mean of per-subtest HIB scores across mixed units.

    Uses OB baseline-relative scores (1.0 = percentiles[0] per subtest).
    head_to_head is ignored — kept only for call-site compatibility.
    """
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
        # PTS generate_geometric_mean_result() skips when fewer than 2 tests contribute.
        out[sid] = geometric_mean(arr) if len(arr) >= 2 else None
    return out


def pts_geometric_mean_ob_composite(
    subtests: list[dict[str, Any]],
    system_ids: list[str],
) -> dict[str, float | None] | None:
    """
    Geometric mean of per-subtest OB-relative scores (OB median = 1.0 per subtest).

    Combines mixed units safely; requires matched OB data on every included subtest.
    """
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
