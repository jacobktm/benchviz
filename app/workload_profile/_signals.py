"""Perf-counter and MONITOR sensor signal collection for workload profiling."""

from __future__ import annotations

import statistics
from collections import defaultdict
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


def collect_workload_signals(
    title: str,
    app_version: str,
    config_args: str = "",
    system_ids: list[int] | None = None,
    *,
    option_description: str = "",
    option_scale: str = "",
) -> dict[str, Any]:
    config_args_db = "" if (not config_args or config_args == "default") else config_args

    perf_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(False),
    )
    if app_version:
        perf_q = perf_q.filter(Benchmark.app_version == app_version)
    perf_bms = [b for b in perf_q.all() if is_perf_counter_benchmark(b)]

    perf_values: dict[str, list[float]] = defaultdict(list)
    for bm in perf_bms:
        key = counter_signal_key(bm)
        if not key:
            continue
        res_q = BenchmarkResult.query.filter_by(benchmark_id=bm.id)
        if system_ids:
            res_q = res_q.filter(BenchmarkResult.system_id.in_(system_ids))
        for res in res_q.all():
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

    sensor_signals: dict[str, float] = {}
    sensor_by_cat: dict[str, list[float]] = defaultdict(list)
    for s_bm in sensors:
        label = _sensor_label(s_bm)
        cat = _sensor_category(label)
        res_q = BenchmarkResult.query.filter_by(benchmark_id=s_bm.id)
        if system_ids:
            res_q = res_q.filter(BenchmarkResult.system_id.in_(system_ids))
        vals: list[float] = []
        for res in res_q.all():
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

    sensor_category_means = {
        cat: statistics.median(vs) for cat, vs in sensor_by_cat.items() if vs
    }

    return {
        "perf": perf_signals,
        "sensors": sensor_signals,
        "sensor_categories": sensor_category_means,
    }


def collect_workload_signals_by_system(
    title: str,
    app_version: str,
    config_args: str = "",
    system_ids: list[int] | None = None,
    *,
    option_description: str = "",
    option_scale: str = "",
) -> dict[int, dict[str, Any]]:
    config_args_db = "" if (not config_args or config_args == "default") else config_args

    perf_by_sys: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    perf_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(False),
    )
    if app_version:
        perf_q = perf_q.filter(Benchmark.app_version == app_version)
    for bm in [b for b in perf_q.all() if is_perf_counter_benchmark(b)]:
        key = counter_signal_key(bm)
        if not key:
            continue
        res_q = BenchmarkResult.query.filter_by(benchmark_id=bm.id)
        if system_ids:
            res_q = res_q.filter(BenchmarkResult.system_id.in_(system_ids))
        for res in res_q.all():
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

    sensor_cat_by_sys: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
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
    for s_bm in sensors:
        cat = _sensor_category(_sensor_label(s_bm))
        if not cat:
            continue
        res_q = BenchmarkResult.query.filter_by(benchmark_id=s_bm.id)
        if system_ids:
            res_q = res_q.filter(BenchmarkResult.system_id.in_(system_ids))
        for res in res_q.all():
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
