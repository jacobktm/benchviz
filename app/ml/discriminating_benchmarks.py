"""
Find benchmarks that best discriminate between two hardware configurations.
Uses Cohen's d (standardized mean difference) to rank benchmarks.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

from app import db
from app.analyzer import INSIGHT_COMPONENT_KEYS
from app.components import get_system_components
from app.models import Benchmark, BenchmarkResult, System
from app.pts import proportion_is_lower_better
from app.repositories import BenchmarkRepository

MIN_SYSTEMS_PER_GROUP = 2


def _cohens_d(scores_a: list[float], scores_b: list[float]) -> float:
    """Cohen's d = (mean_a - mean_b) / pooled_stdev."""
    n1, n2 = len(scores_a), len(scores_b)
    if n1 < 2 or n2 < 2:
        return 0.0
    mean1 = statistics.mean(scores_a)
    mean2 = statistics.mean(scores_b)
    var1 = statistics.variance(scores_a)
    var2 = statistics.variance(scores_b)
    pooled = math.sqrt(((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2))
    if pooled == 0.0:
        if abs(mean1 - mean2) > 1e-12:
            return float('inf') if mean1 > mean2 else float('-inf')
        return 0.0
    return (mean1 - mean2) / pooled


def list_eligible_features() -> list[dict[str, Any]]:
    """Return hardware feature keys that have at least two distinct values (each with MIN_SYSTEMS_PER_GROUP systems)."""
    systems = System.query.all()
    comps_by_sid: dict[int, dict[str, str]] = {}
    for s in systems:
        comps_by_sid[s.id] = get_system_components(s)

    features: list[dict[str, Any]] = []
    for key in INSIGHT_COMPONENT_KEYS:
        val_to_sids: dict[str, set[int]] = defaultdict(set)
        for sid, comps in comps_by_sid.items():
            v = (comps.get(key) or '').strip()
            if v:
                val_to_sids[v].add(sid)
        eligible_values = [
            {'value': v, 'n_systems': len(sids)}
            for v, sids in val_to_sids.items()
            if len(sids) >= MIN_SYSTEMS_PER_GROUP
        ]
        if len(eligible_values) >= 2:
            features.append({
                'feature_key': key,
                'values': sorted(eligible_values, key=lambda x: -x['n_systems']),
            })

    from app.route_helpers.compare import COMPARE_BY_OPTIONS
    label_map = dict(COMPARE_BY_OPTIONS)
    for f in features:
        f['label'] = label_map.get(f['feature_key'], f['feature_key'])

    return features


def find_discriminating_benchmarks(
    feature_key: str,
    value_a: str,
    value_b: str,
    args_str: str = 'default',
    min_groups: int = 2,
    top_k: int = 50,
) -> dict[str, Any]:
    """
    For a hardware feature (e.g. ``processor``), find benchmarks that best
    separate systems with *value_a* from systems with *value_b*.

    Returns benchmarks sorted by absolute Cohen's d (descending).
    """
    args_db = '' if (not args_str or args_str.lower() == 'default') else args_str

    # Find systems in each group
    all_systems = System.query.all()
    group_a: list[System] = []
    group_b: list[System] = []
    for s in all_systems:
        comps = get_system_components(s)
        v = (comps.get(feature_key) or '').strip()
        if v == value_a:
            group_a.append(s)
        elif v == value_b:
            group_b.append(s)

    if len(group_a) < min_groups or len(group_b) < min_groups:
        return {
            'available': False,
            'reason': (
                f'need at least {min_groups} systems per group '
                f'(have {len(group_a)} for {value_a}, {len(group_b)} for {value_b})'
            ),
        }

    a_ids = [s.id for s in group_a]
    b_ids = [s.id for s in group_b]

    # Find common benchmarks across both groups
    def _system_benchmark_titles(system_ids: list[int]) -> dict[tuple[str, str], dict[str, Any]]:
        results = (
            BenchmarkResult.query
            .filter(
                BenchmarkResult.system_id.in_(system_ids),
                BenchmarkResult.value.isnot(None),
                BenchmarkResult.arguments == args_db,
            )
            .all()
        )
        bm_ids = list({r.benchmark_id for r in results})
        benchmarks = {b.id: b for b in Benchmark.query.filter(Benchmark.id.in_(bm_ids)).all()}

        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for r in results:
            bm = benchmarks.get(r.benchmark_id)
            if not bm or bm.display_format != 'BAR_GRAPH' or not bm.is_primary:
                continue
            key = (bm.title, bm.app_version or '')
            if key not in by_key:
                proportion = bm.proportion or ''
                by_key[key] = {
                    'benchmark_title': bm.title,
                    'app_version': bm.app_version or '',
                    'scores': [],
                    'proportion': proportion,
                    'scale': bm.scale or '',
                }
            by_key[key]['scores'].append(float(r.value))
        return by_key

    a_benchmarks = _system_benchmark_titles(a_ids)
    b_benchmarks = _system_benchmark_titles(b_ids)

    common_keys = sorted(set(a_benchmarks.keys()) & set(b_benchmarks.keys()))
    if not common_keys:
        return {'available': False, 'reason': 'no common benchmarks between the two groups'}

    results: list[dict[str, Any]] = []
    for key in common_keys:
        a_info = a_benchmarks[key]
        b_info = b_benchmarks[key]
        scores_a = a_info['scores']
        scores_b = b_info['scores']

        n_a = len(scores_a)
        n_b = len(scores_b)
        if n_a < min_groups or n_b < min_groups:
            continue

        mean_a = statistics.mean(scores_a)
        mean_b = statistics.mean(scores_b)

        d = _cohens_d(scores_a, scores_b)

        is_lower_better = proportion_is_lower_better(a_info.get('proportion') or b_info.get('proportion') or '')
        if is_lower_better:
            # For LIB: lower mean = better. Flip d so positive always favors value_a.
            d = -d

        results.append({
            'benchmark_title': key[0],
            'app_version': key[1],
            'cohens_d': round(d, 4),
            'abs_cohens_d': round(abs(d), 4),
            'mean_a': round(mean_a, 4),
            'mean_b': round(mean_b, 4),
            'n_a': n_a,
            'n_b': n_b,
            'scale': a_info['scale'],
            'is_lower_better': is_lower_better,
            'favors_a': d > 0,
        })

    results.sort(key=lambda r: -r['abs_cohens_d'])
    results = results[:top_k]

    from app.route_helpers.compare import COMPARE_BY_OPTIONS
    label_map = dict(COMPARE_BY_OPTIONS)

    return {
        'available': True,
        'feature_key': feature_key,
        'feature_label': label_map.get(feature_key, feature_key),
        'value_a': value_a,
        'value_b': value_b,
        'n_a': len(group_a),
        'n_b': len(group_b),
        'benchmarks': results,
    }
