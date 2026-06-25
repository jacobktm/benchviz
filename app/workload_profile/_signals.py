"""Perf-counter and MONITOR sensor signal collection for workload profiling."""

from __future__ import annotations

import statistics
from collections import OrderedDict, defaultdict
from typing import Any

from .. import db
from ..models import Benchmark, BenchmarkResult
from ..sensor_quality import _sensor_category, is_noisy_sensor_series, peak_series_value, sensor_kind
from ._helpers import (
    _norm_text,
    _result_matches_option,
    _sensor_label,
    counter_signal_key,
    is_perf_counter_benchmark,
)

_SIGNALS_CACHE: OrderedDict[tuple, Any] = OrderedDict()
_SIGNALS_CACHE_MAX = 64


def _signals_cache_key(
    title: str, app_version: str, config_args: str,
    system_ids: list[int] | None,
    option_description: str, option_scale: str,
) -> tuple:
    return (
        title, app_version, config_args,
        tuple(sorted(system_ids)) if system_ids else None,
        option_description, option_scale,
    )


def _try_track_signals_cache(hit: bool) -> None:
    try:
        from flask import current_app
        metrics = current_app.extensions.get("benchviz_metrics")
        if metrics is not None:
            counter = metrics.signals_cache
            if hit:
                counter.hit()
            else:
                counter.miss()
    except Exception:
        pass


def _cached_signals(key: tuple) -> Any | None:
    val = _SIGNALS_CACHE.get(key)
    if val is not None:
        _SIGNALS_CACHE.move_to_end(key)
        _try_track_signals_cache(hit=True)
    else:
        _try_track_signals_cache(hit=False)
    return val


def _store_signals_cache(key: tuple, val: Any) -> None:
    _SIGNALS_CACHE[key] = val
    if len(_SIGNALS_CACHE) > _SIGNALS_CACHE_MAX:
        _SIGNALS_CACHE.popitem(last=False)


def _clear_signals_cache() -> None:
    _SIGNALS_CACHE.clear()


def _batch_perf_results(
    perf_bms: list[Benchmark],
    system_ids: list[int] | None,
    config_args_db: str,
    option_description: str,
    option_scale: str,
) -> dict[str, list[float]]:
    if not perf_bms:
        return {}
    perf_ids = [bm.id for bm in perf_bms]
    key_map = {bm.id: counter_signal_key(bm) for bm in perf_bms}

    q = BenchmarkResult.query.filter(BenchmarkResult.benchmark_id.in_(perf_ids))
    if system_ids:
        q = q.filter(BenchmarkResult.system_id.in_(system_ids))
    all_results = q.all()

    perf_values: dict[str, list[float]] = defaultdict(list)
    for res in all_results:
        key = key_map.get(res.benchmark_id)
        if not key:
            continue
        if not _result_matches_option(
            res.arguments, config_args_db, option_description, option_scale,
        ):
            continue
        if res.value is None:
            continue
        try:
            perf_values[key].append(float(res.value))
        except (TypeError, ValueError):
            pass
    return perf_values


def _batch_sensor_signals(
    sensors: list[Benchmark],
    system_ids: list[int] | None,
    config_args_db: str,
    option_description: str,
    option_scale: str,
) -> tuple[dict[str, float], dict[str, list[float]]]:
    if not sensors:
        return {}, {}
    sensor_ids = [bm.id for bm in sensors]
    label_map = {bm.id: _sensor_label(bm) for bm in sensors}
    cat_map = {bm.id: _sensor_category(label_map[bm.id]) for bm in sensors}

    q = BenchmarkResult.query.filter(BenchmarkResult.benchmark_id.in_(sensor_ids))
    if system_ids:
        q = q.filter(BenchmarkResult.system_id.in_(system_ids))
    all_results = q.all()

    results_by_bm: dict[int, list[BenchmarkResult]] = defaultdict(list)
    for res in all_results:
        results_by_bm[res.benchmark_id].append(res)

    sensor_signals: dict[str, float] = {}
    sensor_by_cat: dict[str, list[float]] = defaultdict(list)

    for s_bm in sensors:
        label = label_map.get(s_bm.id)
        cat = cat_map.get(s_bm.id)
        if not label:
            continue
        vals: list[float] = []
        for res in results_by_bm.get(s_bm.id, []):
            if not _result_matches_option(
                res.arguments, config_args_db, option_description, option_scale,
            ):
                continue
            if not res.data_json:
                continue
            if is_noisy_sensor_series(res.data_json, s_bm.description, s_bm.scale):
                continue
            kind = sensor_kind(s_bm.description, s_bm.scale)
            if kind in ("usage", "frequency"):
                peak = peak_series_value(res.data_json)
                if peak is not None:
                    vals.append(peak)
            else:
                nums = [float(x) for x in res.data_json if isinstance(x, (int, float))]
                if nums:
                    vals.append(statistics.mean(nums))
        if not vals:
            continue
        med = statistics.median(vals)
        sensor_signals[label] = med
        if cat:
            sensor_by_cat[cat].append(med)

    return sensor_signals, sensor_by_cat


def collect_workload_signals(
    title: str,
    app_version: str,
    config_args: str = "",
    system_ids: list[int] | None = None,
    *,
    option_description: str = "",
    option_scale: str = "",
    _no_cache: bool = False,
) -> dict[str, Any]:
    if not _no_cache:
        key = _signals_cache_key(
            title, app_version, config_args, system_ids,
            option_description, option_scale,
        )
        cached = _cached_signals(key)
        if cached is not None:
            return cached

    config_args_db = "" if (not config_args or config_args == "default") else config_args

    perf_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(False),
    )
    if app_version:
        perf_q = perf_q.filter(Benchmark.app_version == app_version)
    perf_bms = [b for b in perf_q.all() if is_perf_counter_benchmark(b)]

    perf_values = _batch_perf_results(
        perf_bms, system_ids, config_args_db, option_description, option_scale,
    )

    perf_signals = {
        k: statistics.median(vs)
        for k, vs in perf_values.items()
        if vs
    }

    sensor_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "LINE_GRAPH",
    )
    if app_version:
        sensor_q = sensor_q.filter(Benchmark.app_version == app_version)
    sensor_keywords = (
        "temperature", "frequency", "usage", "power", "celsius", "mhz", "watts",
        "fan", "rpm", "voltage", "energy", "utilization",
    )
    sensors = [
        s for s in sensor_q.all()
        if s.description and any(k in s.description.lower() for k in sensor_keywords)
    ]

    sensor_signals, sensor_by_cat = _batch_sensor_signals(
        sensors, system_ids, config_args_db, option_description, option_scale,
    )

    sensor_category_means = {
        cat: statistics.median(vs) for cat, vs in sensor_by_cat.items() if vs
    }

    result = {
        "perf": perf_signals,
        "sensors": sensor_signals,
        "sensor_categories": sensor_category_means,
    }
    if not _no_cache:
        _store_signals_cache(key, result)
    return result


def collect_workload_signals_by_system(
    title: str,
    app_version: str,
    config_args: str = "",
    system_ids: list[int] | None = None,
    *,
    option_description: str = "",
    option_scale: str = "",
    _no_cache: bool = False,
) -> dict[int, dict[str, Any]]:
    if not _no_cache:
        key = _signals_cache_key(
            title, app_version, config_args, system_ids,
            option_description, option_scale,
        )
        cached = _cached_signals(key)
        if cached is not None:
            return cached

    config_args_db = "" if (not config_args or config_args == "default") else config_args

    perf_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(False),
    )
    if app_version:
        perf_q = perf_q.filter(Benchmark.app_version == app_version)
    perf_bms = [b for b in perf_q.all() if is_perf_counter_benchmark(b)]

    perf_by_sys: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    if perf_bms:
        perf_ids = [bm.id for bm in perf_bms]
        key_map = {bm.id: counter_signal_key(bm) for bm in perf_bms}
        q = BenchmarkResult.query.filter(BenchmarkResult.benchmark_id.in_(perf_ids))
        if system_ids:
            q = q.filter(BenchmarkResult.system_id.in_(system_ids))
        for res in q.all():
            key = key_map.get(res.benchmark_id)
            if not key:
                continue
            if not _result_matches_option(
                res.arguments, config_args_db, option_description, option_scale,
            ):
                continue
            if res.value is None:
                continue
            try:
                perf_by_sys[res.system_id][key].append(float(res.value))
            except (TypeError, ValueError):
                pass

    sensor_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "LINE_GRAPH",
    )
    if app_version:
        sensor_q = sensor_q.filter(Benchmark.app_version == app_version)
    sensor_keywords = (
        "temperature", "frequency", "usage", "power", "celsius", "mhz", "watts",
        "fan", "rpm", "voltage", "energy", "utilization",
    )
    sensors = [
        s for s in sensor_q.all()
        if s.description and any(k in s.description.lower() for k in sensor_keywords)
    ]

    sensor_cat_by_sys: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    if sensors:
        sensor_ids = [bm.id for bm in sensors]
        label_map = {bm.id: _sensor_label(bm) for bm in sensors}
        cat_map = {bm.id: _sensor_category(label_map[bm.id]) for bm in sensors}

        q = BenchmarkResult.query.filter(BenchmarkResult.benchmark_id.in_(sensor_ids))
        if system_ids:
            q = q.filter(BenchmarkResult.system_id.in_(system_ids))
        results_by_bm: dict[int, list[BenchmarkResult]] = defaultdict(list)
        for res in q.all():
            results_by_bm[res.benchmark_id].append(res)

        for s_bm in sensors:
            cat = cat_map.get(s_bm.id)
            if not cat:
                continue
            for res in results_by_bm.get(s_bm.id, []):
                if not _result_matches_option(
                    res.arguments, config_args_db, option_description, option_scale,
                ):
                    continue
                if not res.data_json:
                    continue
                if is_noisy_sensor_series(res.data_json, s_bm.description, s_bm.scale):
                    continue
                kind = sensor_kind(s_bm.description, s_bm.scale)
                if kind in ("usage", "frequency"):
                    val = peak_series_value(res.data_json)
                else:
                    nums = [float(x) for x in res.data_json if isinstance(x, (int, float))]
                    val = statistics.mean(nums) if nums else None
                if val is not None:
                    sensor_cat_by_sys[res.system_id][cat].append(float(val))

    all_ids = set(perf_by_sys.keys()) | set(sensor_cat_by_sys.keys())
    if system_ids:
        all_ids |= set(system_ids)

    out: dict[int, dict[str, Any]] = {}
    for sid in all_ids:
        perf_signals = {
            k: statistics.median(vs)
            for k, vs in perf_by_sys.get(sid, {}).items()
            if vs
        }
        sensor_category_means = {
            c: statistics.median(vs)
            for c, vs in sensor_cat_by_sys.get(sid, {}).items()
            if vs
        }
        out[sid] = {
            "perf": perf_signals,
            "sensor_categories": sensor_category_means,
        }
    if not _no_cache:
        _store_signals_cache(key, out)
    return out


def _pool_signals(by_system: dict[int, dict[str, Any]], system_ids: list[int]) -> dict[str, Any]:
    perf_pooled: dict[str, list[float]] = defaultdict(list)
    cat_pooled: dict[str, list[float]] = defaultdict(list)
    for sid in system_ids:
        sig = by_system.get(sid) or {}
        for k, v in (sig.get("perf") or {}).items():
            perf_pooled[k].append(v)
        for k, v in (sig.get("sensor_categories") or {}).items():
            cat_pooled[k].append(v)
    return {
        "perf": {k: statistics.median(vs) for k, vs in perf_pooled.items() if vs},
        "sensors": {},
        "sensor_categories": {k: statistics.median(vs) for k, vs in cat_pooled.items() if vs},
    }
