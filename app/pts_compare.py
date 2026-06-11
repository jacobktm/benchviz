"""Build PTS-style comparison payloads for BenchViz compare API."""

from __future__ import annotations

import re
from typing import Any

from .ob_cache_sync import compare_ob_live_fetch_enabled, load_ob_cache_index, lookup_ob_entry_with_fallback
from .pts_comparison import (
    comparison_hash_for_benchmark,
    lib_to_hib_value,
    normalize_relative_values,
    ob_median_from_entry,
    ob_p1_from_entry,
    ob_percentiles_for_systems,
    pts_geometric_mean_composite,
    pts_geometric_mean_ob_composite,
    pts_harmonic_mean_by_scale,
    pts_harmonic_mean_cross_scale,
    relative_vs_ob_baseline,
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
    if "lower" in pl and "better" in pl:
        return False
    if "higher" in pl and "better" in pl:
        return True
    if "more" in pl and "better" in pl:
        return True
    if "lower" in pl:
        return False
    return "higher" in pl or "more" in pl


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
        ob_entry, ob_source = lookup_ob_entry_with_fallback(
            comp_hash,
            ob_index,
            identifier=identifier,
            title=title,
            arguments=config_args,
            description=desc,
            app_version=app_version,
            scale=scale,
            allow_live=compare_ob_live_fetch_enabled(),
        )
        ob_median = ob_median_from_entry(ob_entry)
        ob_p1 = ob_p1_from_entry(ob_entry)
        relative = normalize_relative_values(values, hib=hib)
        ob_relative = relative_vs_ob_median(values, hib=hib, ob_median=ob_median)
        ob_p1_relative = relative_vs_ob_baseline(values, hib=hib, baseline=ob_p1)
        percentiles = ob_percentiles_for_systems(values, ob_entry)

        per_subtest.append({
            "comparison_hash": comp_hash,
            "description": desc,
            "scale": scale,
            "hib": hib,
            "ob": {
                "matched": ob_entry is not None,
                "source": ob_source or None,
                "fallback": ob_source == "fallback",
                "fallback_app_version": (ob_entry or {}).get("app_version") if ob_source == "fallback" else None,
                "requested_app_version": (ob_entry or {}).get("requested_app_version") if ob_source == "fallback" else None,
                "live_fetched_profile": (ob_entry or {}).get("live_fetched_profile") if ob_source == "live" else None,
                "samples": (ob_entry or {}).get("samples"),
                "unit": (ob_entry or {}).get("unit"),
                "test_profile": (ob_entry or {}).get("test_profile"),
                "median": ob_median,
                "p1": ob_p1,
            },
            "values": values,
            "pts_relative": relative,
            "pts_ob_relative": ob_relative,
            "pts_ob_p1_relative": ob_p1_relative,
            "ob_percentile": percentiles,
        })
        subtest_value_maps.append(values)

    geo_raw = pts_geometric_mean_composite(subtest_value_maps, system_ids, hib_flags)

    geo_for_norm = {sid: geo_raw.get(sid) for sid in system_ids}
    geo_relative = normalize_relative_values(geo_for_norm, hib=True)

    geo_ob_relative = pts_geometric_mean_ob_composite(per_subtest, system_ids)

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
        "geometric_mean_ob_relative": geo_ob_relative,
        "geometric_mean_of_relative_subtests": geo_mean_relative,
        "harmonic_mean_by_scale": harmonic_by_scale,
        "reference_system_id": ref_id,
        "ob_index_synced_at": (ob_index or {}).get("synced_at"),
    }


def build_pts_global_summary(
    comparison_groups: list[dict[str, Any]],
    system_ids: list[str] | None = None,
    *,
    pts_contexts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    PTS generate_geometric_mean_result() across all BAR_GRAPH subtests in the comparison.

    composite_ob uses OB-relative subtest scores when pts_contexts are available.
    composite_raw / composite_relative remain head-to-head fallbacks for raw display.
    """
    trace_ids = system_ids or compare_system_trace_ids(comparison_groups)
    subtests = extract_subtests_from_comparison_groups(comparison_groups)
    per_system: dict[str, list[float]] = {sid: [] for sid in trace_ids}
    subtest_count = 0
    lib_rows = 0
    native_scales: set[str] = set()
    for st in subtests:
        hib = st.get("hib", True)
        scale = (st.get("scale") or "").strip()
        if scale:
            native_scales.add(scale)
        values = st.get("values") or {}
        contributed = False
        for sid in trace_ids:
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
            if not hib:
                lib_rows += 1

    composite_raw = {
        sid: geometric_mean(vs) if len(vs) >= 2 else None
        for sid, vs in per_system.items()
    }
    composite_relative = normalize_relative_values(composite_raw, hib=True)

    ref_id = trace_ids[0] if trace_ids else ""
    min_v = None
    for sid in trace_ids:
        c = composite_raw.get(sid)
        if c is None:
            continue
        if min_v is None or c < min_v:
            min_v = c
            ref_id = sid

    composite_ob = None
    if pts_contexts and trace_ids:
        ob_summary = build_pts_ob_global_summary(pts_contexts, trace_ids)
        composite_ob = ob_summary.get("composite_ob_relative")

    return {
        "composite_raw": composite_raw,
        "composite_relative": composite_relative,
        "composite_ob": composite_ob,
        "reference_system_id": ref_id,
        "subtest_count": subtest_count,
        "lib_inverted": lib_rows > 0,
        "native_scales": sorted(native_scales),
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


def _canonical_system_id(trace_name: str | None, group: dict[str, Any]) -> str:
    name = (trace_name or "").strip()
    if not name:
        return ""
    for s in group.get("system_details") or []:
        sn = (s.get("short_name") or "").strip()
        if not sn:
            continue
        if name == sn or name.startswith(sn + " ") or name.startswith(sn + "("):
            return sn
    m = re.match(r"^(\S+)\s+\([^()]+\)$", name)
    return m.group(1) if m else name


def compare_system_ids(comparison_groups: list[dict[str, Any]]) -> list[str]:
    """Canonical system short_names for the comparison (stable order)."""
    ids: list[str] = []
    seen: set[str] = set()
    for group in comparison_groups:
        for s in group.get("system_details") or []:
            sid = (s.get("short_name") or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                ids.append(sid)
    return ids


def extract_subtests_from_comparison_groups(
    comparison_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One subtest per primary chart row (matches compare UI config aggregation)."""
    subtests: list[dict[str, Any]] = []
    for group in comparison_groups:
        for ch in group.get("charts") or []:
            if not ch.get("is_primary"):
                continue
            scale = (ch.get("scale") or "").strip()
            hib = _is_hib(ch.get("proportion"))
            values: dict[str, float | None] = {}
            for tr in ch.get("traces") or []:
                name = tr.get("name")
                if name is None:
                    continue
                sys_id = _canonical_system_id(str(name), group)
                if not sys_id:
                    continue
                y = tr.get("y")
                raw = y[0] if isinstance(y, list) and y else y
                try:
                    values[sys_id] = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    values[sys_id] = None
            subtests.append({
                "hib": hib,
                "scale": scale,
                "values": values,
            })
    return subtests


def compare_system_trace_ids(comparison_groups: list[dict[str, Any]]) -> list[str]:
    """System keys used in chart trace values (canonical short_names)."""
    ids = compare_system_ids(comparison_groups)
    if ids:
        return ids
    for group in comparison_groups:
        for ch in group.get("charts") or []:
            if not ch.get("is_primary"):
                continue
            traces = ch.get("traces") or []
            canon = [_canonical_system_id(str(t["name"]), group) for t in traces if t.get("name") is not None]
            canon = [c for c in canon if c]
            if canon:
                return canon
    return []


def build_pts_global_harmonic_summary(
    comparison_groups: list[dict[str, Any]],
    system_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Harmonic summaries: per-scale raw buckets plus cross-unit relative overall."""
    trace_ids = system_ids or compare_system_trace_ids(comparison_groups)
    subtests = extract_subtests_from_comparison_groups(comparison_groups)
    cross_scale = pts_harmonic_mean_cross_scale(subtests, trace_ids, head_to_head=False)
    if cross_scale is None:
        cross_scale = pts_harmonic_mean_cross_scale(subtests, trace_ids, head_to_head=True)
    return {
        "by_scale": pts_harmonic_mean_by_scale(subtests, trace_ids),
        "cross_scale": cross_scale,
    }
