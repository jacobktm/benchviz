"""API endpoints for Performance Insights — variance, scatter, cohort spread, explanations."""

from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict

from flask import jsonify, request

from sqlalchemy import func as sqla_func

from app import db
from app.models import Benchmark, BenchmarkAnalysis, BenchmarkResult
from app.pts import proportion_is_lower_better
from app.repositories import BenchmarkRepository, SystemRepository
from app.components import get_system_components, get_primary_group_name
from app.result_merge import bar_run_values
from app.route_helpers import (
    _load_primary_insights_bundle,
    _insights_signal_to_noise_raw,
    _insights_alignment_tier,
    _insights_alignment_rank_score,
    _insights_eta_squared_norm_buckets,
    _insights_infer_scope,
    _insights_workload_context_from_analysis,
    _insights_allowed_singles_for_scope,
    format_system_profile_label,
    COMPARE_BY_OPTIONS,
)

from . import bp


@bp.route('/api/scatter_candidates')
def api_scatter_candidates():
    from app.analyzer import INSIGHT_COMPONENT_KEYS

    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()
    top_k = int(request.args.get('top_k') or 10)
    min_points = int(request.args.get('min_points') or 3)
    min_distinct_x = int(request.args.get('min_distinct_x') or 2)
    min_effect = float(request.args.get('min_effect') or 0.1)

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400

    primary_bms = BenchmarkRepository.find_primary_by_title(title, app_version)
    if not primary_bms:
        return {"error": "No primary BAR_GRAPH benchmark found for the given title/app_version"}, 404
    primary_bm_ids = [b.id for b in primary_bms]

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    results = (
        BenchmarkResult.query
        .filter(
            BenchmarkResult.benchmark_id.in_(primary_bm_ids),
            BenchmarkResult.arguments == args_str,
            BenchmarkResult.value.isnot(None),
        )
        .all()
    )
    if not results:
        return {"candidates": [], "meta": {"points": 0}}, 200

    by_system = defaultdict(list)
    for r in results:
        by_system[r.system_id].append(r.value)

    sys_ids = sorted(by_system.keys())
    systems = SystemRepository.find_by_ids(sys_ids)
    comps = {s.id: get_system_components(s) for s in systems}

    y_raw_by_system = {}
    y_by_system = {}
    for sid, vals in by_system.items():
        y_raw = statistics.mean(vals)
        y_raw_by_system[sid] = y_raw
        y_by_system[sid] = y_raw * y_flip

    def robust_spread(vals):
        if not vals:
            return 0.0
        m = statistics.median(vals)
        abs_dev = [abs(v - m) for v in vals]
        return statistics.median(abs_dev) or 0.0

    def spearman_rho(x, y):
        n = len(x)
        if n < 3:
            return None
        def rank(arr):
            pairs = sorted((v, i) for i, v in enumerate(arr))
            ranks = [0.0] * n
            k = 0
            while k < n:
                v = pairs[k][0]
                j = k
                while j < n and pairs[j][0] == v:
                    j += 1
                avg = (k + 1 + j) / 2.0
                for t in range(k, j):
                    ranks[pairs[t][1]] = avg
                k = j
            return ranks
        rx = rank(x)
        ry = rank(y)
        mx = statistics.mean(rx)
        my = statistics.mean(ry)
        num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        denx = sum((a - mx) ** 2 for a in rx) ** 0.5
        deny = sum((b - my) ** 2 for b in ry) ** 0.5
        if denx == 0 or deny == 0:
            return None
        return num / (denx * deny)

    def parse_version_numeric(s):
        if not s:
            return None
        s = str(s)
        nums = re.findall(r'\d+', s)
        if not nums:
            return None
        n0 = int(nums[0])
        n1 = int(nums[1]) if len(nums) > 1 else 0
        n2 = int(nums[2]) if len(nums) > 2 else 0
        return float(n0) + (n1 / 1000.0) + (n2 / 1_000_000.0)

    def score_numeric(points):
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        spread = robust_spread(ys) or 1e-9
        uniq_x = sorted(set(xs))
        k = min(5, max(2, len(uniq_x)))
        if k < 2:
            return None
        xs_sorted = sorted(points, key=lambda t: t[0])
        bin_means = []
        for b in range(k):
            lo = int(b * len(points) / k)
            hi = int((b + 1) * len(points) / k)
            if hi <= lo:
                continue
            bin_vals = xs_sorted[lo:hi]
            if not bin_vals:
                continue
            bin_means.append(statistics.mean([t[1] for t in bin_vals]))
        if len(bin_means) < 2:
            return None
        top = max(bin_means)
        bottom = min(bin_means)
        effect = (top - bottom) / spread
        rho = spearman_rho(xs, ys)
        return {"effect": effect, "rho": rho}

    def score_categorical(points):
        by_x = defaultdict(list)
        for x_raw, y in points:
            by_x[x_raw].append(y)
        if len(by_x) < 2:
            return None
        ys_all = [y for _, y in points]
        spread = robust_spread(ys_all) or 1e-9
        means = [statistics.mean(vs) for vs in by_x.values()]
        top = max(means)
        bottom = min(means)
        effect = (top - bottom) / spread
        return {"effect": effect}

    VERSION_NUMERIC_X_KEYS = {
        "kernel_version",
        "nvidia_driver",
        "mesa_version",
        "llvm_version",
        "vulkan_driver",
    }

    candidates = []
    label_map = dict(COMPARE_BY_OPTIONS)
    for x_key in INSIGHT_COMPONENT_KEYS:
        raw_points = []
        for sid in sys_ids:
            x_raw = (comps.get(sid, {}).get(x_key) or '').strip()
            if not x_raw:
                continue
            y = y_by_system.get(sid)
            if y is None:
                continue
            raw_points.append((sid, x_raw, y))

        if len(raw_points) < min_points:
            continue
        distinct_x = len({p[1] for p in raw_points})
        if distinct_x < min_distinct_x:
            continue

        numeric_points = []
        numeric_parsed = 0
        categorical_points = []
        for sid, x_raw, y in raw_points:
            x_num = parse_version_numeric(x_raw)
            if x_num is not None:
                numeric_parsed += 1
                numeric_points.append((x_num, y))
            categorical_points.append((x_raw, y))

        numeric_mode = (x_key in VERSION_NUMERIC_X_KEYS) and (numeric_parsed >= 3) and (numeric_parsed / max(1, len(raw_points)) >= 0.8)

        if numeric_mode:
            scored = score_numeric(numeric_points)
            if not scored:
                continue
            effect = scored.get("effect")
            if effect is None or effect < min_effect:
                continue

            points_out = []
            for sid, x_raw, y in raw_points:
                points_out.append({
                    "system_id": sid,
                    "x": x_raw,
                    "x_numeric": parse_version_numeric(x_raw),
                    "y": y_raw_by_system.get(sid),
                    "y_raw": y_raw_by_system.get(sid),
                    "y_normalized": y,
                })

            candidates.append({
                "x_key": x_key,
                "x_label": label_map.get(x_key, x_key),
                "type": "numeric",
                "effect_score": effect,
                "spearman_rho": scored.get("rho"),
                "point_count": len(raw_points),
                "distinct_x": distinct_x,
                "points": points_out,
            })
        else:
            scored = score_categorical(categorical_points)
            if not scored:
                continue
            effect = scored.get("effect")
            if effect is None or effect < min_effect:
                continue

            points_out = []
            for sid, x_raw, y in raw_points:
                points_out.append({
                    "system_id": sid,
                    "x": x_raw,
                    "y": y_raw_by_system.get(sid),
                    "y_raw": y_raw_by_system.get(sid),
                    "y_normalized": y,
                })

            candidates.append({
                "x_key": x_key,
                "x_label": label_map.get(x_key, x_key),
                "type": "categorical",
                "effect_score": effect,
                "point_count": len(raw_points),
                "distinct_x": distinct_x,
                "points": points_out,
            })

    candidates.sort(key=lambda c: (c["effect_score"], c.get("spearman_rho") or 0), reverse=True)
    y_label_base = primary_bms[0].scale or "Score"
    lower_better = is_lower_better
    y_label = f"{y_label_base} ({'lower is better' if lower_better else 'higher is better'})"
    return {
        "candidates": candidates[:top_k],
        "meta": {
            "benchmark_title": title,
            "app_version": app_version,
            "args": args_str,
            "primary_benchmark_count": len(primary_bm_ids),
            "systems_with_primary_y": len(y_by_system),
            "min_points": min_points,
            "min_distinct_x": min_distinct_x,
            "min_effect": min_effect,
            "y_axis_label": y_label,
            "y_flip": y_flip,
        }
    }, 200


@bp.route('/api/variance_feature_map')
def api_variance_feature_map():
    from app.analyzer import INSIGHT_COMPONENT_KEYS, MIN_SYSTEMS_TOTAL, MIN_SYSTEMS_PER_COHORT

    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()

    top_k = int(request.args.get('top_k') or 10)
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    include_pairs = (request.args.get('include_pairs') or '1').lower() not in {'0', 'false', 'no'}
    min_feature_delta = float(request.args.get('min_feature_delta') or 0.0)

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400

    primary_bms = BenchmarkRepository.find_primary_by_title(title, app_version)
    if not primary_bms:
        return {"error": "No primary BAR_GRAPH benchmark found for the given title/app_version"}, 404

    rep_bm = primary_bms[0]
    label_map = dict(COMPARE_BY_OPTIONS)
    y_label_base = rep_bm.scale or "Score"

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    args_analysis_key = 'default' if (not args_str or args_str.lower() == 'default') else args_str
    args_db = '' if args_analysis_key == 'default' else args_str

    all_results = BenchmarkResult.query.filter(
        BenchmarkResult.benchmark_id.in_([b.id for b in primary_bms]),
        BenchmarkResult.arguments == args_db,
        BenchmarkResult.value.isnot(None),
    ).all()

    if not all_results:
        return {"points": [], "meta": {"y_label": y_label_base, "x_label": "within-system run variability (stdev)"}}, 200

    by_system_run_vals = defaultdict(list)
    for r in all_results:
        by_system_run_vals[r.system_id].extend(bar_run_values(r.data_json, r.value))

    if not by_system_run_vals:
        return {"points": [], "meta": {"y_label": y_label_base, "x_label": "within-system run variability (stdev)"}}, 200

    y_raw_mean_by_system = {}
    y_norm_mean_by_system = {}
    within_system_std_by_system = {}
    for sid, run_vals in by_system_run_vals.items():
        y_raw_mean = statistics.mean(run_vals)
        y_raw_mean_by_system[sid] = y_raw_mean
        y_norm_mean_by_system[sid] = y_raw_mean * y_flip
        within_system_std_by_system[sid] = statistics.stdev(run_vals) if len(run_vals) >= 2 else 0.0

    sys_ids = sorted(y_raw_mean_by_system.keys())
    systems = SystemRepository.find_by_ids(sys_ids)
    comps_by_sid = {s.id: get_system_components(s) for s in systems}

    points = []

    def add_feature_point(feature_type, feature_key, system_groups):
        if not system_groups:
            return
        total_systems_with_feature = sum(len(v) for v in system_groups.values())
        if total_systems_with_feature < MIN_SYSTEMS_TOTAL:
            return
        valid_groups = []
        for cohort_val, sys_summaries in system_groups.items():
            if len(sys_summaries) < min_cohort_n:
                continue
            valid_groups.append((cohort_val, sys_summaries))
        if len(valid_groups) < 2:
            return
        group_rows = []
        for cohort_val, sys_summaries in valid_groups:
            mean_raw = statistics.mean([t[0] for t in sys_summaries])
            mean_norm = statistics.mean([t[1] for t in sys_summaries])
            avg_within_std = statistics.mean([t[2] for t in sys_summaries]) if sys_summaries else 0.0
            group_rows.append((cohort_val, mean_raw, mean_norm, avg_within_std))
        best_row = max(group_rows, key=lambda r: r[2])
        worst_row = min(group_rows, key=lambda r: r[2])
        _, best_mean_raw, _, _ = best_row
        _, worst_mean_raw, _, _ = worst_row
        dominance_delta_raw = abs(best_mean_raw - worst_mean_raw)
        if dominance_delta_raw < min_feature_delta:
            return
        within_system_var_avg = statistics.mean([r[3] for r in group_rows]) if group_rows else 0.0
        dominance_score = dominance_delta_raw / (within_system_var_avg + 1e-9)
        points.append({
            "feature_type": feature_type,
            "feature_key": feature_key,
            "feature_label": label_map.get(feature_key, feature_key),
            "x_within_spread": within_system_var_avg,
            "y_dominance_delta_raw": dominance_delta_raw,
            "dominance_score": dominance_score,
            "distinct_cohort_values": len(group_rows),
            "systems_with_feature": total_systems_with_feature,
            "best_mean_raw": best_mean_raw,
            "worst_mean_raw": worst_mean_raw,
        })

    for feature_key in INSIGHT_COMPONENT_KEYS:
        system_groups = defaultdict(list)
        systems_with_feature = set()
        for sid, mean_raw in y_raw_mean_by_system.items():
            v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
            if not v:
                continue
            systems_with_feature.add(sid)
            system_groups[v].append((
                mean_raw,
                y_norm_mean_by_system.get(sid, mean_raw * y_flip),
                within_system_std_by_system.get(sid, 0.0),
            ))
        if len(systems_with_feature) < MIN_SYSTEMS_TOTAL:
            continue
        add_feature_point("single", feature_key, system_groups)

    if include_pairs:
        pair_defs = [
            ("processor", "memory"),
            ("processor", "cooler_model"),
            ("processor", "graphics"),
            ("graphics", "memory"),
        ]
        for k1, k2 in pair_defs:
            pair_groups = defaultdict(list)
            systems_with_pair = set()
            for sid, mean_raw in y_raw_mean_by_system.items():
                c1 = (comps_by_sid.get(sid, {}).get(k1) or '').strip()
                c2 = (comps_by_sid.get(sid, {}).get(k2) or '').strip()
                if not c1 or not c2:
                    continue
                systems_with_pair.add(sid)
                pair_groups[(c1, c2)].append((
                    mean_raw,
                    y_norm_mean_by_system.get(sid, mean_raw * y_flip),
                    within_system_std_by_system.get(sid, 0.0),
                ))
            if len(systems_with_pair) < MIN_SYSTEMS_TOTAL:
                continue
            feature_key = f"{k1}+{k2}"
            feature_label = f"{label_map.get(k1,k1)} + {label_map.get(k2,k2)}"
            add_feature_point("pair", feature_key, pair_groups)
            if points:
                points[-1]["feature_label"] = feature_label

    points.sort(key=lambda p: p["dominance_score"], reverse=True)
    points = points[:top_k]

    return {
        "points": points,
        "meta": {
            "benchmark_title": title,
            "app_version": app_version,
            "args": args_analysis_key,
            "y_label": y_label_base,
            "x_label": "within-system run variability (stddev of runs)",
            "direction": "y_dominance_delta_raw is best-worst mean diff across cohorts (always positive); x is within-system run noise",
            "y_flip": y_flip,
            "min_cohort_n": min_cohort_n,
            "min_feature_delta": min_feature_delta,
        }
    }, 200


@bp.route('/api/variance_leaderboard')
def api_variance_leaderboard():
    from app.analyzer import INSIGHT_COMPONENT_KEYS, MIN_SYSTEMS_TOTAL

    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()

    top_k = int(request.args.get('top_k') or 10)
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_distinct_cohorts = int(request.args.get('min_distinct_cohorts') or 2)
    include_pairs = (request.args.get('include_pairs') or '1').lower() not in {'0', 'false', 'no'}

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400

    primary_bms = BenchmarkRepository.find_primary_by_title(title, app_version)
    if not primary_bms:
        return {"error": "No primary BAR_GRAPH benchmark found for the given title/app_version"}, 404

    rep_bm = primary_bms[0]
    label_map = dict(COMPARE_BY_OPTIONS)
    y_label_base = rep_bm.scale or "Score"

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    args_analysis_key = 'default' if (not args_str or args_str.lower() == 'default') else args_str
    args_db = '' if args_analysis_key == 'default' else args_str

    primary_bm_ids = [b.id for b in primary_bms]
    all_results = BenchmarkResult.query.filter(
        BenchmarkResult.benchmark_id.in_(primary_bm_ids),
        BenchmarkResult.arguments == args_db,
        BenchmarkResult.value.isnot(None),
    ).all()

    if not all_results:
        return {
            "rows": [],
            "meta": {
                "benchmark_title": title,
                "app_version": app_version,
                "args": args_analysis_key,
                "y_label": y_label_base,
                "x_label": "within-bucket spread",
                "min_cohort_n": min_cohort_n,
                "include_pairs": include_pairs,
            }
        }, 200

    by_system_vals = defaultdict(list)
    for r in all_results:
        by_system_vals[r.system_id].append(r.value)

    y_norm_by_system = {}
    y_raw_by_system = {}
    for sid, vals in by_system_vals.items():
        y_raw = statistics.mean([v for v in vals if v is not None])
        y_raw_by_system[sid] = y_raw
        y_norm_by_system[sid] = y_raw * y_flip

    sys_ids = sorted(y_raw_by_system.keys())
    systems = SystemRepository.find_by_ids(sys_ids)
    comps_by_sid = {s.id: get_system_components(s) for s in systems}

    all_y_norm = [y_norm_by_system[sid] for sid in sys_ids]

    def robust_spread(vals):
        if not vals:
            return 0.0
        m = statistics.median(vals)
        abs_dev = [abs(v - m) for v in vals]
        return statistics.median(abs_dev) or 0.0

    overall_spread = robust_spread(all_y_norm)
    overall_spread_eps = overall_spread + 1e-9

    text_blob = " ".join([
        (rep_bm.title or ""),
        (rep_bm.description or ""),
        args_str or "",
    ]).lower()
    wl_ctx = _insights_workload_context_from_analysis(title, app_version, args_analysis_key, text_blob)
    scope = wl_ctx["scope"]
    active_bottlenecks = list(wl_ctx.get("active_bottlenecks") or [])
    if scope == "general" and not active_bottlenecks:
        inferred = _insights_infer_scope(text_blob)
        if inferred != "general":
            scope = inferred
            active_bottlenecks = [inferred]
    scope_override = (request.args.get('scope') or '').strip().lower()
    include_all_component_keys = scope_override == "all"
    if scope_override in {"all", "general"}:
        scope = "general"
        active_bottlenecks = []
    elif scope_override in {"cpu", "gpu", "storage", "memory"}:
        scope = scope_override
        active_bottlenecks = [scope_override]
    allowed_singles = _insights_allowed_singles_for_scope(
        scope, include_all_component_keys, active_bottlenecks or None,
    )

    rows = []

    def eval_single(feature_key):
        buckets = defaultdict(list)
        for sid in sys_ids:
            v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
            if not v:
                continue
            buckets[v].append(y_norm_by_system[sid])
        systems_with_nonempty = sum(len(ys) for ys in buckets.values())
        if systems_with_nonempty < MIN_SYSTEMS_TOTAL:
            return
        if len(buckets) < min_distinct_cohorts:
            return
        if not any(len(ys) >= 2 for ys in buckets.values()):
            return
        vals = []
        for ys in buckets.values():
            vals.extend(ys)
        grand_mean = statistics.mean(vals)
        ss_total = sum((y - grand_mean) ** 2 for y in vals)
        if ss_total < 1e-18:
            return
        ss_between = 0.0
        for ys in buckets.values():
            nj = len(ys)
            mj = statistics.mean(ys)
            ss_between += nj * (mj - grand_mean) ** 2
        eta_sq = ss_between / ss_total
        bucket_spreads = []
        for ys in buckets.values():
            s = robust_spread(ys)
            bucket_spreads.append((s, len(ys)))
        total_w = sum(n for _, n in bucket_spreads) or 1
        conditional_spread = sum(s * n for s, n in bucket_spreads) / total_w
        reduction_ratio = 1.0 - (conditional_spread / overall_spread_eps)
        cohorts_meeting_min_n = sum(1 for ys in buckets.values() if len(ys) >= min_cohort_n)
        rows.append({
            "feature_type": "single",
            "feature_key": feature_key,
            "feature_label": label_map.get(feature_key, feature_key),
            "eta_squared": eta_sq,
            "reduction_ratio": reduction_ratio,
            "overall_spread": overall_spread,
            "conditional_spread": conditional_spread,
            "distinct_cohort_values": len(buckets),
            "systems_with_feature": systems_with_nonempty,
            "cohorts_meeting_min_n": cohorts_meeting_min_n,
            "min_cohort_n": min_cohort_n,
        })

    def eval_pair(k1, k2):
        buckets = defaultdict(list)
        for sid in sys_ids:
            c1 = (comps_by_sid.get(sid, {}).get(k1) or '').strip()
            c2 = (comps_by_sid.get(sid, {}).get(k2) or '').strip()
            if not c1 or not c2:
                continue
            buckets[(c1, c2)].append(y_norm_by_system[sid])
        systems_with_pair = sum(len(ys) for ys in buckets.values())
        if systems_with_pair < MIN_SYSTEMS_TOTAL:
            return
        if len(buckets) < min_distinct_cohorts:
            return
        if not any(len(ys) >= 2 for ys in buckets.values()):
            return
        vals = []
        for ys in buckets.values():
            vals.extend(ys)
        grand_mean = statistics.mean(vals)
        ss_total = sum((y - grand_mean) ** 2 for y in vals)
        if ss_total < 1e-18:
            return
        ss_between = 0.0
        for ys in buckets.values():
            nj = len(ys)
            mj = statistics.mean(ys)
            ss_between += nj * (mj - grand_mean) ** 2
        eta_sq = ss_between / ss_total
        bucket_spreads = []
        for ys in buckets.values():
            s = robust_spread(ys)
            bucket_spreads.append((s, len(ys)))
        total_w = sum(n for _, n in bucket_spreads) or 1
        conditional_spread = sum(s * n for s, n in bucket_spreads) / total_w
        reduction_ratio = 1.0 - (conditional_spread / overall_spread_eps)
        cohorts_meeting_min_n = sum(1 for ys in buckets.values() if len(ys) >= min_cohort_n)
        rows.append({
            "feature_type": "pair",
            "feature_key": f"{k1}+{k2}",
            "feature_label": f"{label_map.get(k1,k1)} + {label_map.get(k2,k2)}",
            "eta_squared": eta_sq,
            "reduction_ratio": reduction_ratio,
            "overall_spread": overall_spread,
            "conditional_spread": conditional_spread,
            "distinct_cohort_values": len(buckets),
            "systems_with_feature": systems_with_pair,
            "cohorts_meeting_min_n": cohorts_meeting_min_n,
            "min_cohort_n": min_cohort_n,
        })

    for feature_key in allowed_singles:
        eval_single(feature_key)

    if include_pairs:
        if scope == "cpu":
            pair_defs = [("processor", "memory"), ("processor", "cooler_model")]
        elif scope == "gpu":
            pair_defs = [("processor", "graphics"), ("graphics", "memory")]
        elif scope == "storage":
            pair_defs = [("processor", "memory")]
        else:
            pair_defs = [("processor", "memory"), ("processor", "cooler_model"), ("processor", "graphics"), ("graphics", "memory")]
        for k1, k2 in pair_defs:
            eval_pair(k1, k2)

    rows.sort(
        key=lambda r: (r["eta_squared"], r["cohorts_meeting_min_n"], r["reduction_ratio"]),
        reverse=True,
    )
    rows = rows[:top_k]

    return {
        "rows": rows,
        "meta": {
            "benchmark_title": title,
            "app_version": app_version,
            "args": args_analysis_key,
            "y_label": y_label_base,
            "x_label": "between-cohort share of variance (eta²)",
            "overall_spread": overall_spread,
            "overall_spread_eps": overall_spread_eps,
            "min_cohort_n": min_cohort_n,
            "min_distinct_cohorts": min_distinct_cohorts,
            "include_pairs": include_pairs,
            "feature_scope": scope,
            "workload_source": wl_ctx.get("source"),
            "workload_bottlenecks": active_bottlenecks,
            "workload_proportions": wl_ctx.get("score_proportions") or {},
            "ranking": "eta_squared_primary",
            "require_replicated_cohort": True,
        }
    }, 200


@bp.route('/api/insights_eligible_groupby')
def api_insights_eligible_groupby():
    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()
    scope_override = (request.args.get('scope') or '').strip().lower()
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_distinct_cohorts = int(request.args.get('min_distinct_cohorts') or 2)

    bundle, err = _load_primary_insights_bundle(title, app_version, args_str, scope_override)
    if err:
        return {"error": err[0]}, err[1]

    label_map = bundle["label_map"]
    y_norm_by_system = bundle["y_norm_by_system"]
    y_raw_by_system = bundle["y_raw_by_system"]
    sys_ids = bundle["sys_ids"]
    comps_by_sid = bundle["comps_by_sid"]
    min_systems_total = bundle["MIN_SYSTEMS_TOTAL"]

    features = []
    for feature_key in bundle["allowed_singles"]:
        buckets_sid = defaultdict(list)
        for sid in sys_ids:
            v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
            if not v:
                continue
            buckets_sid[v].append(sid)

        buckets = defaultdict(list)
        for v, sids in buckets_sid.items():
            for sid in sids:
                buckets[v].append(y_norm_by_system[sid])

        systems_with_nonempty = sum(len(ys) for ys in buckets.values())
        if systems_with_nonempty < min_systems_total:
            continue
        if len(buckets) < min_distinct_cohorts:
            continue
        if not any(len(ys) >= min_cohort_n for ys in buckets.values()):
            continue

        vals = []
        for ys in buckets.values():
            vals.extend(ys)
        grand_mean = statistics.mean(vals)
        ss_total = sum((y - grand_mean) ** 2 for y in vals)
        if ss_total < 1e-18:
            continue
        ss_between = 0.0
        for ys in buckets.values():
            nj = len(ys)
            mj = statistics.mean(ys)
            ss_between += nj * (mj - grand_mean) ** 2
        eta_sq = ss_between / ss_total

        sn_ratio, spread_raw = _insights_signal_to_noise_raw(buckets_sid, y_raw_by_system)
        tier, tier_summary = _insights_alignment_tier(eta_sq, sn_ratio)
        rank_score = _insights_alignment_rank_score(eta_sq, sn_ratio)

        features.append({
            "feature_key": feature_key,
            "feature_label": label_map.get(feature_key, feature_key),
            "eta_squared": eta_sq,
            "signal_to_noise": sn_ratio,
            "cohort_mean_spread_raw": spread_raw,
            "alignment_tier": tier,
            "alignment_summary": tier_summary,
            "alignment_rank_score": rank_score,
            "n_distinct_values": len(buckets),
            "max_cohort_size": max(len(ys) for ys in buckets.values()),
            "n_systems": systems_with_nonempty,
        })

    features.sort(key=lambda x: (x["alignment_rank_score"], x["eta_squared"]), reverse=True)
    for i, row in enumerate(features, start=1):
        row["alignment_rank"] = i

    return {
        "features": features,
        "meta": {
            "benchmark_title": bundle["title"],
            "app_version": bundle["app_version"],
            "args": bundle["args_analysis_key"],
            "feature_scope": bundle["scope"],
            "workload_source": (bundle.get("workload_context") or {}).get("source"),
            "workload_bottlenecks": list((bundle.get("workload_context") or {}).get("active_bottlenecks") or []),
            "workload_proportions": (bundle.get("workload_context") or {}).get("score_proportions") or {},
            "min_cohort_n": min_cohort_n,
            "min_distinct_cohorts": min_distinct_cohorts,
            "alignment_ranking_note": (
                "alignment_rank_score blends eta² (between-cohort variance share) and "
                "signal-to-noise (spread of cohort means vs median within-cohort stdev). "
                "Tiers are heuristics for association, not proof a part drives the score."
            ),
        },
    }, 200


@bp.route('/api/insights_cohort_spread')
def api_insights_cohort_spread():
    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()
    feature_key = (request.args.get('feature_key') or '').strip()
    scope_override = (request.args.get('scope') or '').strip().lower()
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_distinct_cohorts = int(request.args.get('min_distinct_cohorts') or 2)

    if not feature_key:
        return {"error": "Missing feature_key query parameter"}, 400

    bundle, err = _load_primary_insights_bundle(title, app_version, args_str, scope_override)
    if err:
        return {"error": err[0]}, err[1]

    if feature_key not in bundle["allowed_singles"]:
        return {"error": "feature_key is not allowed for this benchmark scope"}, 400

    y_raw_by_system = bundle["y_raw_by_system"]
    sys_ids = bundle["sys_ids"]
    comps_by_sid = bundle["comps_by_sid"]
    systems_by_id = bundle["systems_by_id"]
    min_systems_total = bundle["MIN_SYSTEMS_TOTAL"]

    buckets = defaultdict(list)
    for sid in sys_ids:
        v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
        if not v:
            continue
        buckets[v].append((sid, y_raw_by_system[sid]))

    systems_with_nonempty = sum(len(pairs) for pairs in buckets.values())
    if systems_with_nonempty < min_systems_total:
        return {"error": "Insufficient systems with this feature populated"}, 400
    if len(buckets) < min_distinct_cohorts:
        return {"error": "Need at least min_distinct_cohorts values for this feature"}, 400
    if not any(len(pairs) >= min_cohort_n for pairs in buckets.values()):
        return {"error": "Need at least one cohort with min_cohort_n systems"}, 400

    is_lower_better = bundle["is_lower_better"]
    cohort_rows = []
    for v, pairs in buckets.items():
        ys_raw = [p[1] for p in pairs]
        mean_raw = statistics.mean(ys_raw)
        stdev_raw = statistics.stdev(ys_raw) if len(ys_raw) > 1 else 0.0
        cohort_rows.append({
            "value": v,
            "n": len(pairs),
            "mean_raw": mean_raw,
            "stdev_raw": stdev_raw,
            "systems": [
                {
                    "system_id": sid,
                    "label": format_system_profile_label(systems_by_id[sid]),
                    "y_raw": yr,
                }
                for sid, yr in pairs
            ],
        })

    cohort_rows.sort(key=lambda c: c["mean_raw"], reverse=not is_lower_better)
    means = [c["mean_raw"] for c in cohort_rows]
    spread = (max(means) - min(means)) if means else 0.0
    inner_stds = [c["stdev_raw"] for c in cohort_rows if c["n"] > 1]
    med_inner = statistics.median(inner_stds) if inner_stds else 0.0
    sn_ratio = float(spread / (med_inner + 1e-9))

    norm_by_val = defaultdict(list)
    y_norm_by_system = bundle["y_norm_by_system"]
    for c in cohort_rows:
        for s in c["systems"]:
            norm_by_val[c["value"]].append(y_norm_by_system[s["system_id"]])
    eta_sel = _insights_eta_squared_norm_buckets(norm_by_val)
    tier, tier_summary = _insights_alignment_tier(eta_sel, sn_ratio)

    pairwise = []
    for i in range(len(cohort_rows)):
        for j in range(i + 1, len(cohort_rows)):
            hi, lo = cohort_rows[i], cohort_rows[j]
            pairwise.append({
                "rank_a": i + 1,
                "rank_b": j + 1,
                "cohort_a_value": hi["value"],
                "cohort_b_value": lo["value"],
                "mean_a_raw": hi["mean_raw"],
                "mean_b_raw": lo["mean_raw"],
                "mean_gap_raw": abs(hi["mean_raw"] - lo["mean_raw"]),
                "note": "Rank 1 is best performance for this benchmark (raw units).",
            })

    from app.hardware_ranks import theoretical_alignment_payload
    theoretical_alignment = theoretical_alignment_payload(feature_key, cohort_rows, is_lower_better)

    return {
        "feature_key": feature_key,
        "feature_label": bundle["label_map"].get(feature_key, feature_key),
        "cohorts": cohort_rows,
        "pairwise_ordered": pairwise,
        "theoretical_alignment": theoretical_alignment,
        "meta": {
            "benchmark_title": bundle["title"],
            "app_version": bundle["app_version"],
            "args": bundle["args_analysis_key"],
            "y_label": bundle["y_label_base"],
            "is_lower_better": is_lower_better,
            "feature_scope": bundle["scope"],
            "cohort_mean_spread_raw": spread,
            "signal_to_noise": sn_ratio,
            "eta_squared": eta_sel,
            "alignment_tier": tier,
            "alignment_summary": tier_summary,
        },
    }, 200


@bp.route('/api/variance_leaderboard_coverage')
def api_variance_leaderboard_coverage():
    from app.analyzer import INSIGHT_COMPONENT_KEYS, MIN_SYSTEMS_TOTAL as _INS_MIN_SYSTEMS

    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_distinct_cohorts = int(request.args.get('min_distinct_cohorts') or 2)

    primary_pairs = set(
        (b.title, b.app_version or '')
        for b in BenchmarkRepository.find_all_primary()
    )
    analyses = [
        a for a in BenchmarkAnalysis.query.all()
        if (a.benchmark_title, a.benchmark_app_version or '') in primary_pairs
    ]

    buckets = defaultdict(set)

    for a in analyses:
        if not a.analysis_json:
            continue
        b_title = a.benchmark_title
        b_app = a.benchmark_app_version or ''

        for args_key, feature_stats in (a.analysis_json or {}).items():
            if not isinstance(feature_stats, dict) or str(args_key).startswith("_"):
                continue

            has_any_feature = False
            for feature_key, feature_values in feature_stats.items():
                if not isinstance(feature_values, list) or not feature_values:
                    continue
                if feature_values[0].get('error'):
                    continue
                cohorts = [
                    v for v in feature_values
                    if isinstance(v, dict) and not v.get('error') and (v.get('n') or 0) >= 1
                ]
                if len(cohorts) < min_distinct_cohorts:
                    continue
                if sum((v.get('n') or 0) for v in cohorts) < _INS_MIN_SYSTEMS:
                    continue
                has_any_feature = True
                break

            if has_any_feature:
                buckets[(b_title, b_app)].add(args_key)

    cov_rows = (
        db.session.query(
            Benchmark.title,
            Benchmark.app_version,
            BenchmarkResult.arguments,
            sqla_func.count(sqla_func.distinct(BenchmarkResult.system_id)).label("n_sys"),
        )
        .join(BenchmarkResult, BenchmarkResult.benchmark_id == Benchmark.id)
        .filter(
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary.is_(True),
            BenchmarkResult.value.isnot(None),
        )
        .group_by(Benchmark.title, Benchmark.app_version, BenchmarkResult.arguments)
        .having(sqla_func.count(sqla_func.distinct(BenchmarkResult.system_id)) >= _INS_MIN_SYSTEMS)
        .all()
    )
    for t, av, arg, _n in cov_rows:
        pair = (t, av or "")
        if pair not in primary_pairs:
            continue
        cfg_key = "default" if (arg is None or str(arg).strip() == "") else str(arg).strip()
        buckets[pair].add(cfg_key)

    out = []
    for (b_title, b_app), args_set in sorted(buckets.items(), key=lambda t: (t[0][0], t[0][1])):
        out.append({
            "benchmark_title": b_title,
            "app_version": b_app,
            "args": sorted(list(args_set)),
        })

    return {"benchmarks": out, "meta": {"min_cohort_n": min_cohort_n, "min_distinct_cohorts": min_distinct_cohorts}}, 200


@bp.route('/api/explain_underperformance')
def api_explain_underperformance():
    from app.analyzer import INSIGHT_COMPONENT_KEYS, MIN_SYSTEMS_TOTAL, MIN_SYSTEMS_PER_COHORT

    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()
    system_id_raw = request.args.get('system_id')

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400
    if not system_id_raw:
        return {"error": "Missing system_id query parameter"}, 400
    try:
        system_id = int(system_id_raw)
    except (ValueError, TypeError):
        return {"error": "Invalid system_id"}, 400

    top_n_components = int(request.args.get('top_n_components') or 6)
    top_n_pairs = int(request.args.get('top_n_pairs') or 3)
    include_pairs = (request.args.get('include_pairs') or '1').lower() not in {'0', 'false', 'no'}
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_pair_n = int(request.args.get('min_pair_n') or 2)

    label_map = dict(COMPARE_BY_OPTIONS)

    primary_bms = BenchmarkRepository.find_primary_by_title(title, app_version)
    if not primary_bms:
        return {"error": "No primary BAR_GRAPH benchmark found for this title/app_version"}, 404
    primary_bm_ids = [b.id for b in primary_bms]

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    args_analysis_key = 'default' if (not args_str or args_str.lower() == 'default') else args_str
    args_db = '' if args_analysis_key == 'default' else args_str

    all_results = BenchmarkResult.query.filter(
        BenchmarkResult.benchmark_id.in_(primary_bm_ids),
        BenchmarkResult.arguments == args_db,
        BenchmarkResult.value.isnot(None),
    ).all()

    if not all_results:
        return {"error": "No BAR_GRAPH results found for this benchmark/config"}, 404

    by_system_vals = defaultdict(list)
    for r in all_results:
        by_system_vals[r.system_id].append(r.value)

    if system_id not in by_system_vals:
        return {"error": "Requested system_id has no results for this benchmark/config"}, 404

    y_raw_by_system = {sid: statistics.mean(vals) for sid, vals in by_system_vals.items()}
    y_norm_by_system = {sid: y_raw * y_flip for sid, y_raw in y_raw_by_system.items()}

    sys_ids = list(y_raw_by_system.keys())
    systems = SystemRepository.find_by_ids(sys_ids)
    comps_by_sid = {s.id: get_system_components(s) for s in systems}
    system_comps = comps_by_sid.get(system_id, {})

    system_y_raw = y_raw_by_system[system_id]
    system_y_norm = y_norm_by_system[system_id]
    best_system_norm = max(y_norm_by_system.values())
    worst_system_norm = min(y_norm_by_system.values())
    gap_to_best_system = best_system_norm - system_y_norm

    feature_explanations = []
    for feature_key in INSIGHT_COMPONENT_KEYS:
        value_to_norm_scores = defaultdict(list)
        systems_with_feature = set()

        for sid, y_norm in y_norm_by_system.items():
            v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
            if not v:
                continue
            systems_with_feature.add(sid)
            value_to_norm_scores[v].append(y_norm)

        total_systems_with_feature = len(systems_with_feature)
        if total_systems_with_feature < MIN_SYSTEMS_TOTAL:
            continue

        valid_values = []
        for v, norm_scores in value_to_norm_scores.items():
            n_systems_for_value = len(norm_scores)
            if n_systems_for_value < min_cohort_n:
                continue
            valid_values.append((v, statistics.mean(norm_scores), n_systems_for_value))

        if len(valid_values) < 2:
            continue

        system_value = (system_comps.get(feature_key) or '').strip()
        if not system_value:
            continue

        best_mean_norm = max(m for _, m, _ in valid_values)
        system_entry = next((e for e in valid_values if e[0] == system_value), None)
        if not system_entry:
            continue

        _, system_mean_norm, n_systems_for_value = system_entry
        delta_to_best_cohort = best_mean_norm - system_mean_norm
        if delta_to_best_cohort <= 0:
            continue

        feature_explanations.append({
            "feature_key": feature_key,
            "feature_label": label_map.get(feature_key, feature_key),
            "feature_value": system_value,
            "cohort_mean_normalized": system_mean_norm,
            "best_cohort_mean_normalized": best_mean_norm,
            "delta_to_best_cohort_normalized": delta_to_best_cohort,
            "n_systems_for_cohort_value": n_systems_for_value,
            "n_systems_with_feature": total_systems_with_feature,
        })

    feature_explanations.sort(key=lambda x: x["delta_to_best_cohort_normalized"], reverse=True)
    feature_explanations = feature_explanations[:top_n_components]

    pair_explanations = []
    if include_pairs:
        pair_defs = [
            ("processor", "memory"),
            ("processor", "cooler_model"),
            ("processor", "graphics"),
            ("graphics", "memory"),
        ]

        for k1, k2 in pair_defs:
            pair_to_norm_scores = defaultdict(list)
            systems_with_pair = set()

            for sid, y_norm in y_norm_by_system.items():
                c1 = (comps_by_sid.get(sid, {}).get(k1) or '').strip()
                c2 = (comps_by_sid.get(sid, {}).get(k2) or '').strip()
                if not c1 or not c2:
                    continue
                systems_with_pair.add(sid)
                pair_to_norm_scores[(c1, c2)].append(y_norm)

            total_systems_with_pair = len(systems_with_pair)
            if total_systems_with_pair < MIN_SYSTEMS_TOTAL or len(pair_to_norm_scores) < 2:
                continue

            valid_pairs = []
            for pair_tuple, norm_scores in pair_to_norm_scores.items():
                n_systems_for_pair = len(norm_scores)
                if n_systems_for_pair < min_pair_n:
                    continue
                valid_pairs.append((pair_tuple, statistics.mean(norm_scores), n_systems_for_pair))

            if len(valid_pairs) < 2:
                continue

            s1 = (system_comps.get(k1) or '').strip()
            s2 = (system_comps.get(k2) or '').strip()
            if not s1 or not s2:
                continue

            best_pair_mean_norm = max(m for _, m, _ in valid_pairs)
            system_pair_entry = next((e for e in valid_pairs if e[0] == (s1, s2)), None)
            if not system_pair_entry:
                continue

            _, system_pair_mean_norm, n_systems_for_pair = system_pair_entry
            delta_to_best_pair_normalized = best_pair_mean_norm - system_pair_mean_norm
            if delta_to_best_pair_normalized <= 0:
                continue

            pair_explanations.append({
                "pair_keys": [k1, k2],
                "pair_label": f"{label_map.get(k1,k1)} + {label_map.get(k2,k2)}",
                "pair_values": [s1, s2],
                "pair_mean_normalized": system_pair_mean_norm,
                "best_pair_mean_normalized": best_pair_mean_norm,
                "delta_to_best_pair_normalized": delta_to_best_pair_normalized,
                "n_systems_for_pair": n_systems_for_pair,
                "n_systems_with_pair": total_systems_with_pair,
            })

        pair_explanations.sort(key=lambda x: x["delta_to_best_pair_normalized"], reverse=True)
        pair_explanations = pair_explanations[:top_n_pairs]

    return {
        "benchmark_title": title,
        "app_version": app_version,
        "args": args_analysis_key,
        "system_id": system_id,
        "direction": "higher_is_better_after_normalization",
        "y_flip": y_flip,
        "evidence_thresholds": {
            "min_cohort_n": min_cohort_n,
            "min_pair_n": min_pair_n,
            "min_systems_total_with_feature": MIN_SYSTEMS_TOTAL,
        },
        "observed": {
            "y_raw_mean": system_y_raw,
            "y_normalized_mean": system_y_norm,
            "best_system_y_normalized_mean": best_system_norm,
            "worst_system_y_normalized_mean": worst_system_norm,
            "gap_to_best_system_normalized": gap_to_best_system,
        },
        "single_feature_contributors": feature_explanations,
        "pair_contributors": pair_explanations,
    }, 200
