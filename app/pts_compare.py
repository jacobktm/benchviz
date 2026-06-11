"""Build PTS-style comparison payloads for BenchViz compare API."""

from __future__ import annotations

from typing import Any

from .ob_cache_sync import load_ob_cache_index, lookup_ob_entry
from .pts_comparison import (
    comparison_hash_for_benchmark,
    lib_to_hib_value,
    normalize_relative_values,
    ob_median_from_entry,
    ob_percentiles_for_systems,
    pts_geometric_mean_composite,
    pts_harmonic_mean_by_scale,
    relative_vs_ob_median,
    strip_test_profile_identifier,
)
from .pts_math import geometric_mean


def _is_hib(proportion: str | None) -> bool:
    p = (proportion or "").strip().upper()
    if p == "HIB":
        return True
    if p == "LIB":
        return False
    pl = (proportion or "").lower()
    return "higher" in pl and "better" in pl


def build_pts_context_for_compare_group(
    *,
    title: str,
    app_version: str,
    identifier: str | None,
    primary_charts: list[dict[str, Any]],
    system_ids: list[str],
    config_args: str = "",
    ob_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    PTS scoring overlay for one comparison group (one config of one benchmark suite).

    primary_charts: API chart dicts with description, scale, proportion, traces.
    """
    ob_index = ob_index if ob_index is not None else load_ob_cache_index()
    per_subtest: list[dict[str, Any]] = []
    subtest_value_maps: list[dict[str, float | None]] = []
    hib_flags: list[bool] = []

    for ch in primary_charts:
        desc = (ch.get("description") or "").strip()
        scale = (ch.get("scale") or "").strip()
        proportion = ch.get("proportion")
        hib = _is_hib(proportion)
        hib_flags.append(hib)

        values: dict[str, float | None] = {}
        for tr in ch.get("traces") or []:
            name = tr.get("name")
            if name is None:
                continue
            y = tr.get("y")
            raw = y[0] if isinstance(y, list) and y else y
            try:
                values[str(name)] = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                values[str(name)] = None

        comp_hash = comparison_hash_for_benchmark(
            identifier=identifier,
            title=title,
            description=desc,
            app_version=app_version,
            scale=scale,
            arguments=config_args,
        )
        ob_entry = lookup_ob_entry(comp_hash, ob_index)
        ob_median = ob_median_from_entry(ob_entry)
        relative = normalize_relative_values(values, hib=hib)
        ob_relative = relative_vs_ob_median(values, hib=hib, ob_median=ob_median)
        percentiles = ob_percentiles_for_systems(values, ob_entry)

        per_subtest.append({
            "comparison_hash": comp_hash,
            "description": desc,
            "scale": scale,
            "hib": hib,
            "ob": {
                "matched": ob_entry is not None,
                "samples": (ob_entry or {}).get("samples"),
                "unit": (ob_entry or {}).get("unit"),
                "test_profile": (ob_entry or {}).get("test_profile"),
                "median": ob_median,
            },
            "values": values,
            "pts_relative": relative,
            "pts_ob_relative": ob_relative,
            "ob_percentile": percentiles,
        })
        subtest_value_maps.append(values)

    geo_raw = pts_geometric_mean_composite(subtest_value_maps, system_ids, hib_flags)

    geo_for_norm = {sid: geo_raw.get(sid) for sid in system_ids}
    geo_relative = normalize_relative_values(geo_for_norm, hib=True)

    ref_id = system_ids[0] if system_ids else ""
    best_geo = None
    for sid in system_ids:
        g = geo_raw.get(sid)
        if g is None:
            continue
        if best_geo is None or g < best_geo:
            best_geo = g
            ref_id = sid

    rel_scores = []
    for st in per_subtest:
        for sid in system_ids:
            r = (st.get("pts_relative") or {}).get(sid)
            if r is not None and r > 0:
                rel_scores.append((sid, r))
    per_system_rel: dict[str, list[float]] = {sid: [] for sid in system_ids}
    for sid, r in rel_scores:
        per_system_rel[sid].append(r)
    geo_mean_relative = {
        sid: geometric_mean(vs) if vs else None
        for sid, vs in per_system_rel.items()
    }

    harmonic_by_scale = pts_harmonic_mean_by_scale(per_subtest, system_ids)

    return {
        "test_identifier": strip_test_profile_identifier(identifier) or title,
        "subtests": per_subtest,
        "geometric_mean_raw": geo_raw,
        "geometric_mean_relative": geo_relative,
        "geometric_mean_of_relative_subtests": geo_mean_relative,
        "harmonic_mean_by_scale": harmonic_by_scale,
        "reference_system_id": ref_id,
        "ob_index_synced_at": (ob_index or {}).get("synced_at"),
    }


def build_pts_global_summary(
    group_contexts: list[dict[str, Any]],
    system_ids: list[str],
) -> dict[str, Any]:
    """
    PTS generate_geometric_mean_result() across all BAR_GRAPH subtests in the comparison.

    Raw values are LIB-inverted to HIB scale per subtest, geo-meaned per system, then
    normalized via normalize_buffer_values() (reference = slowest).
    """
    per_system: dict[str, list[float]] = {sid: [] for sid in system_ids}
    subtest_count = 0
    for ctx in group_contexts:
        for st in ctx.get("subtests") or []:
            hib = st.get("hib", True)
            values = st.get("values") or {}
            contributed = False
            for sid in system_ids:
                raw = values.get(sid)
                if raw is None:
                    continue
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    continue
                if v <= 0:
                    continue
                per_system[sid].append(lib_to_hib_value(v) if not hib else v)
                contributed = True
            if contributed:
                subtest_count += 1

    composite_raw = {
        sid: geometric_mean(vs) if len(vs) >= 2 else None
        for sid, vs in per_system.items()
    }
    composite_relative = normalize_relative_values(composite_raw, hib=True)

    ref_id = system_ids[0] if system_ids else ""
    min_v = None
    for sid in system_ids:
        c = composite_raw.get(sid)
        if c is None:
            continue
        if min_v is None or c < min_v:
            min_v = c
            ref_id = sid

    return {
        "composite_raw": composite_raw,
        "composite_relative": composite_relative,
        "reference_system_id": ref_id,
        "subtest_count": subtest_count,
    }


def build_pts_ob_global_summary(
    group_contexts: list[dict[str, Any]],
    system_ids: list[str],
) -> dict[str, Any]:
    """
    Geo-mean of per-subtest OB-relative scores (1.0 = OB median per subtest).

    Matches comparing each result against the OpenBenchmarking population median.
    """
    per_system: dict[str, list[float]] = {sid: [] for sid in system_ids}
    matched_subtests = 0
    for ctx in group_contexts:
        for st in ctx.get("subtests") or []:
            if not (st.get("ob") or {}).get("matched"):
                continue
            rel = st.get("pts_ob_relative") or {}
            contributed = False
            for sid in system_ids:
                v = rel.get(sid)
                if v is not None and v > 0:
                    per_system[sid].append(float(v))
                    contributed = True
            if contributed:
                matched_subtests += 1

    composite = {
        sid: geometric_mean(vs) if len(vs) >= 2 else None
        for sid, vs in per_system.items()
    }
    return {
        "composite_ob_relative": composite,
        "subtest_count": matched_subtests,
    }


def build_pts_global_harmonic_summary(
    group_contexts: list[dict[str, Any]],
    system_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """PTS harmonic mean across all comparison groups, grouped by result scale."""
    all_subtests: list[dict[str, Any]] = []
    for ctx in group_contexts:
        all_subtests.extend(ctx.get("subtests") or [])
    return pts_harmonic_mean_by_scale(all_subtests, system_ids)
