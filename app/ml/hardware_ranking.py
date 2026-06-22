"""
Hardware-adjusted ranking: predict expected scores from hardware specs,
then rank systems by actual / predicted to surface over/underperformers.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from app import db
from app.models import Benchmark, BenchmarkResult, HardwareSpec, System
from app.pts import proportion_is_lower_better
from app.repositories import BenchmarkRepository

from app.ml.features import _collect_perf_for_system, _collect_sensors_for_system

MIN_SYSTEMS_FOR_RANKING = 3


def _extract_hw_features(spec: HardwareSpec | None) -> dict[str, float]:
    """Extract numerical features from a HardwareSpec."""
    features: dict[str, float] = {}
    if spec is None:
        return features

    if spec.cpu_cores is not None:
        features['cpu_cores'] = float(spec.cpu_cores)
    if spec.cpu_threads is not None:
        features['cpu_threads'] = float(spec.cpu_threads)

    cpu = spec.cpu_spec
    if isinstance(cpu, dict):
        for k in ('boost_clock_mhz', 'base_clock_mhz', 'tdp_watts', 'l3_cache_kb', 'l2_cache_kb'):
            v = cpu.get(k)
            if v is not None:
                try:
                    features[f'cpu_{k}'] = float(v)
                except (TypeError, ValueError):
                    pass

    gpu = spec.gpu_spec
    if isinstance(gpu, dict):
        for k in ('vram_mb', 'boost_clock_mhz', 'core_clock_mhz', 'tdp_watts', 'shader_count'):
            v = gpu.get(k)
            if v is not None:
                try:
                    features[f'gpu_{k}'] = float(v)
                except (TypeError, ValueError):
                    pass

    mem = spec.memory_spec
    if isinstance(mem, dict):
        for k in ('size_mb', 'speed_mhz', 'channels'):
            v = mem.get(k)
            if v is not None:
                try:
                    features[f'memory_{k}'] = float(v)
                except (TypeError, ValueError):
                    pass

    return features


def _available_feature_keys(system_features: dict[int, dict[str, float]]) -> list[str]:
    """Return feature keys present in at least 2 systems."""
    counts: dict[str, int] = {}
    for feats in system_features.values():
        for k in feats:
            counts[k] = counts.get(k, 0) + 1
    return sorted(k for k, c in counts.items() if c >= 2)


def rank_benchmark(
    title: str,
    app_version: str,
    args_str: str = 'default',
) -> dict[str, Any]:
    """
    For a benchmark group, predict expected scores from hardware specs
    via Ridge regression and rank systems by their actual / predicted ratio.

    Returns::

        {
            "available": bool,
            "benchmark_title": str,
            "app_version": str,
            "args": str,
            "n_systems": int,
            "n_features": int,
            "feature_keys": list[str],
            "is_lower_better": bool,
            "score_unit": str,
            "r2_score": float,
            "systems": [
                {
                    "system_id": int,
                    "label": str,
                    "actual_score": float,
                    "expected_score": float | None,
                    "ratio": float | None,         # actual / expected (raw)
                    "overperformance": float | None,  # positive = beating expectations
                    "n_features": int,                # how many features this system contributed
                }
            ]
        }
    """
    args_db = '' if (not args_str or args_str.lower() == 'default') else args_str

    primary_bms = BenchmarkRepository.find_primary_by_title(title, app_version)
    if not primary_bms:
        return {'available': False, 'reason': 'no primary benchmarks found'}

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0
    rep = primary_bms[0]
    score_unit = rep.scale or 'score'

    primary_bm_ids = [b.id for b in primary_bms]
    results = (
        BenchmarkResult.query
        .filter(
            BenchmarkResult.benchmark_id.in_(primary_bm_ids),
            BenchmarkResult.arguments == args_db,
            BenchmarkResult.value.isnot(None),
        )
        .all()
    )
    if not results:
        return {'available': False, 'reason': 'no results for this config'}

    by_system: dict[int, list[float]] = defaultdict(list)
    for r in results:
        by_system[r.system_id].append(float(r.value))

    system_ids = sorted(by_system.keys())
    if len(system_ids) < MIN_SYSTEMS_FOR_RANKING:
        return {
            'available': False,
            'reason': f'need at least {MIN_SYSTEMS_FOR_RANKING} systems (have {len(system_ids)})',
        }

    # Load HardwareSpec for each system
    specs = {
        s.system_id: s
        for s in HardwareSpec.query.filter(HardwareSpec.system_id.in_(system_ids)).all()
    }

    # Build feature matrix
    systems_with_spec = [sid for sid in system_ids if sid in specs]
    system_features: dict[int, dict[str, float]] = {}
    for sid in systems_with_spec:
        feats = _extract_hw_features(specs[sid])
        if feats:
            system_features[sid] = feats

    if len(system_features) < MIN_SYSTEMS_FOR_RANKING:
        return {
            'available': False,
            'reason': f'need at least {MIN_SYSTEMS_FOR_RANKING} systems with HardwareSpec data (have {len(system_features)})',
        }

    feature_keys = _available_feature_keys(system_features)
    if not feature_keys:
        return {
            'available': False,
            'reason': 'no shared hardware features across systems',
        }

    # Build X (feature matrix), y (scores)
    feature_idxes = {k: i for i, k in enumerate(feature_keys)}
    n_features = len(feature_keys)
    train_sids: list[int] = []
    X_rows: list[list[float]] = []
    y_vals: list[float] = []

    for sid in sorted(system_features.keys()):
        feats = system_features[sid]
        row = [feats.get(k, 0.0) for k in feature_keys]
        raw_mean = statistics.mean(by_system[sid])
        train_sids.append(sid)
        X_rows.append(row)
        y_vals.append(raw_mean)

    X = np.array(X_rows, dtype=float)
    y = np.array(y_vals, dtype=float)

    # Standardize features
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)

    # Ridge regression
    alpha = 1.0 if n_features < len(train_sids) else 5.0
    model = Ridge(alpha=alpha, random_state=42)
    model.fit(Xs, y)
    r2 = float(model.score(Xs, y))

    y_pred = model.predict(Xs)

    # Build per-system output
    all_systems = SystemRepository.find_by_ids(system_ids)
    systems_by_id = {s.id: s for s in all_systems}

    from app.route_helpers import format_system_profile_label

    predictions: dict[int, float] = {}
    for i, sid in enumerate(train_sids):
        predictions[sid] = float(max(y_pred[i], 0.0))

    output_systems: list[dict[str, Any]] = []
    for sid in system_ids:
        system = systems_by_id.get(sid)
        if not system:
            continue
        actual = statistics.mean(by_system[sid])
        expected = predictions.get(sid)
        ratio: float | None = None
        overperformance: float | None = None
        overperformance_pct: float | None = None
        if expected is not None and expected > 0:
            ratio = actual / expected
            overperformance = (actual - expected) * y_flip
            if is_lower_better:
                overperformance_pct = (expected / actual - 1.0) * 100.0
            else:
                overperformance_pct = (actual / expected - 1.0) * 100.0
        n_feat = len(system_features.get(sid, {}))
        output_systems.append({
            'system_id': sid,
            'label': format_system_profile_label(system),
            'actual_score': round(actual, 4),
            'expected_score': round(expected, 4) if expected is not None else None,
            'ratio': round(ratio, 4) if ratio is not None else None,
            'overperformance': round(overperformance, 4) if overperformance is not None else None,
            'overperformance_pct': round(overperformance_pct, 2) if overperformance_pct is not None else None,
            'n_features': n_feat,
        })

    output_systems.sort(
        key=lambda s: (s['overperformance'] if s['overperformance'] is not None else float('-inf')),
        reverse=True,
    )

    return {
        'available': True,
        'benchmark_title': title,
        'app_version': app_version,
        'args': args_str,
        'n_systems': len(system_ids),
        'n_features': n_features,
        'feature_keys': feature_keys,
        'is_lower_better': is_lower_better,
        'score_unit': score_unit,
        'r2_score': round(r2, 4),
        'alpha': alpha,
        'systems': output_systems,
    }


def compute_system_overperformance(system_id: int) -> dict[str, Any]:
    """
    Aggregate overperformance across all benchmarks for a single system.

    Returns::

        {
            "system_id": int,
            "n_benchmarks_with_data": int,     # benchmarks where ranking was available
            "n_benchmarks_total": int,          # total benchmarks system has results for
            "mean_overperformance_pct": float | None,  # mean of overperformance_pct
            "median_overperformance_pct": float | None,
            "best": { ... } | None,             # benchmark with highest overperformance_pct
            "worst": { ... } | None,            # benchmark with lowest overperformance_pct
            "benchmarks": [
                {
                    "benchmark_title": str,
                    "app_version": str,
                    "overperformance_pct": float | None,
                    "actual_score": float,
                    "expected_score": float | None,
                    "score_unit": str,
                    "is_lower_better": bool,
                }
            ]
        }
    """
    system = db.session.get(System, system_id)
    if not system:
        return {'system_id': system_id, 'error': 'system not found'}

    result_bm_ids = set(r.benchmark_id for r in system.results)
    primary_bms = set(
        b.id for b in Benchmark.query.filter(
            Benchmark.id.in_(result_bm_ids),
            Benchmark.display_format == 'BAR_GRAPH',
            Benchmark.is_primary.is_(True),
        ).all()
    )

    title_version_pairs: set[tuple[str, str]] = set()
    for r in system.results:
        b = r.benchmark
        if b and b.id in primary_bms:
            title_version_pairs.add((b.title, b.app_version or ''))

    total = len(title_version_pairs)

    benchmark_scores: list[dict[str, Any]] = []
    for title, app_version in sorted(title_version_pairs):
        result = rank_benchmark(title, app_version)
        if not result.get('available'):
            continue
        sys_entry = next(
            (s for s in result.get('systems', []) if s['system_id'] == system_id),
            None,
        )
        if sys_entry is None:
            continue
        over_pct = sys_entry.get('overperformance_pct')
        benchmark_scores.append({
            'benchmark_title': title,
            'app_version': app_version,
            'overperformance_pct': over_pct,
            'actual_score': sys_entry['actual_score'],
            'expected_score': sys_entry.get('expected_score'),
            'score_unit': result.get('score_unit', ''),
            'is_lower_better': result.get('is_lower_better', False),
        })

    if not benchmark_scores:
        return {
            'system_id': system_id,
            'n_benchmarks_with_data': 0,
            'n_benchmarks_total': total,
            'mean_overperformance_pct': None,
            'median_overperformance_pct': None,
            'best': None,
            'worst': None,
            'benchmarks': [],
        }

    pcts = [b['overperformance_pct'] for b in benchmark_scores if b['overperformance_pct'] is not None]
    mean_pct = statistics.mean(pcts) if pcts else None
    median_pct = statistics.median(pcts) if pcts else None

    sorted_bm = sorted(benchmark_scores, key=lambda b: b['overperformance_pct'] if b['overperformance_pct'] is not None else 0.0)
    best = sorted_bm[-1] if sorted_bm else None
    worst = sorted_bm[0] if sorted_bm else None

    return {
        'system_id': system_id,
        'n_benchmarks_with_data': len(benchmark_scores),
        'n_benchmarks_total': total,
        'mean_overperformance_pct': round(mean_pct, 2) if mean_pct is not None else None,
        'median_overperformance_pct': round(median_pct, 2) if median_pct is not None else None,
        'best': best,
        'worst': worst,
        'benchmarks': benchmark_scores,
    }


def _collect_sensor_metrics(
    title: str,
    app_version: str,
    args_str: str,
    system_ids: list[int],
) -> dict[int, dict[str, float]]:
    """Collect per-system sensor/perf metrics for a ranking cohort."""
    args_db = '' if (not args_str or args_str.lower() == 'default') else args_str
    out: dict[int, dict[str, float]] = {}
    for sid in system_ids:
        perf = _collect_perf_for_system(title, app_version, args_db, sid)
        sensors = _collect_sensors_for_system(title, app_version, args_db, sid)
        m: dict[str, float] = {}
        for k, v in perf.items():
            m[f'perf_{k}'] = v
        t = sensors.thermal
        if t.cpu_temp_peak is not None:
            m['cpu_temp_peak'] = t.cpu_temp_peak
        if t.cpu_freq_peak is not None:
            m['cpu_freq_peak'] = t.cpu_freq_peak
        if t.cpu_freq_min is not None:
            m['cpu_freq_min'] = t.cpu_freq_min
            if t.cpu_freq_peak is not None:
                m['cpu_freq_droop'] = t.cpu_freq_peak - t.cpu_freq_min
        if t.cpu_power_mean is not None:
            m['cpu_power_mean'] = t.cpu_power_mean
        if t.gpu_temp_peak is not None:
            m['gpu_temp_peak'] = t.gpu_temp_peak
        if t.gpu_freq_peak is not None:
            m['gpu_freq_peak'] = t.gpu_freq_peak
        if t.gpu_power_mean is not None:
            m['gpu_power_mean'] = t.gpu_power_mean
        if sensors.usage.cpu_usage_peak is not None:
            m['cpu_usage_peak'] = sensors.usage.cpu_usage_peak
        if sensors.usage.gpu_usage_peak is not None:
            m['gpu_usage_peak'] = sensors.usage.gpu_usage_peak
        if m:
            out[sid] = m
    return out


_COHORT_METRIC_LABELS: dict[str, str] = {
    'cpu_temp_peak': 'CPU temp peak',
    'cpu_freq_peak': 'CPU freq peak',
    'cpu_freq_droop': 'CPU freq droop',
    'cpu_power_mean': 'CPU power mean',
    'cpu_usage_peak': 'CPU usage peak',
    'gpu_temp_peak': 'GPU temp peak',
    'gpu_freq_peak': 'GPU freq peak',
    'gpu_power_mean': 'GPU power mean',
    'gpu_usage_peak': 'GPU usage peak',
}


def explain_ranking_sensors(
    title: str,
    app_version: str,
    args_str: str = 'default',
    system_id: int | None = None,
) -> dict[str, Any]:
    """
    For a given system in a benchmark ranking, compare its sensor/perf metrics
    to the cohort average and return explanations for over/underperformance.
    """
    ranking = rank_benchmark(title, app_version, args_str)
    if not ranking.get('available'):
        return {'available': False, 'reason': ranking.get('reason', 'ranking not available')}

    system_ids = [s['system_id'] for s in ranking['systems']]
    cohort_metrics = _collect_sensor_metrics(title, app_version, args_str, system_ids)

    if not cohort_metrics:
        return {'available': False, 'reason': 'no sensor data available for these systems'}

    # Compute cohort averages per metric (across all systems)
    metric_sums: dict[str, float] = {}
    metric_counts: dict[str, int] = {}
    for sid, metrics in cohort_metrics.items():
        for k, v in metrics.items():
            metric_sums[k] = metric_sums.get(k, 0.0) + v
            metric_counts[k] = metric_counts.get(k, 0) + 1

    cohort_avg: dict[str, float] = {
        k: metric_sums[k] / metric_counts[k]
        for k in metric_sums
    }

    explanations: list[dict[str, Any]] = []

    if system_id is not None:
        # Single system explanation
        sys_metrics = cohort_metrics.get(system_id, {})
        if not sys_metrics:
            return {'available': False, 'reason': 'no sensor data for this system'}
        for k, cohort_val in sorted(cohort_avg.items()):
            sys_val = sys_metrics.get(k)
            if sys_val is None:
                continue
            diff = sys_val - cohort_val
            diff_pct = (diff / abs(cohort_val)) * 100.0 if cohort_val != 0.0 else 0.0
            explanations.append({
                'metric': k,
                'label': _COHORT_METRIC_LABELS.get(k, k),
                'system_value': round(sys_val, 2),
                'cohort_average': round(cohort_val, 2),
                'difference': round(diff, 2),
                'difference_pct': round(diff_pct, 1),
                'direction': 'high' if diff > 0 else 'low',
            })
        explanations.sort(key=lambda e: abs(e['difference_pct']), reverse=True)
        return {
            'available': True,
            'benchmark_title': title,
            'app_version': app_version,
            'system_id': system_id,
            'n_systems_in_cohort': len(system_ids),
            'explanations': explanations,
        }
    else:
        # Full cohort comparison — return average for each metric
        return {
            'available': True,
            'benchmark_title': title,
            'app_version': app_version,
            'n_systems_in_cohort': len(system_ids),
            'cohort_averages': {k: round(v, 2) for k, v in sorted(cohort_avg.items())},
        }


def list_rankable_benchmarks() -> list[dict[str, Any]]:
    """Return all benchmark groups with enough systems and HardwareSpec data for ranking."""
    primary_bms = BenchmarkRepository.find_all_primary()
    groups: dict[tuple[str, str], list[Benchmark]] = defaultdict(list)
    for bm in primary_bms:
        groups[(bm.title, bm.app_version or '')].append(bm)

    results: list[dict[str, Any]] = []
    for (title, app_version), _ in sorted(groups.items()):
        primary_bm_ids = [b.id for b in BenchmarkRepository.find_primary_by_title(title, app_version)]
        system_ids = set(
            r.system_id
            for r in BenchmarkResult.query
            .filter(
                BenchmarkResult.benchmark_id.in_(primary_bm_ids),
                BenchmarkResult.value.isnot(None),
            )
            .all()
        )
        if len(system_ids) < MIN_SYSTEMS_FOR_RANKING:
            continue

        specs_count = HardwareSpec.query.filter(
            HardwareSpec.system_id.in_(list(system_ids)),
        ).count()

        results.append({
            'benchmark_title': title,
            'app_version': app_version,
            'n_systems': len(system_ids),
            'n_with_spec': specs_count,
        })

    return results
