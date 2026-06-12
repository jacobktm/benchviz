"""
Feature extraction for ML insights: perf counters, MONITOR sensors, hardware, run noise.

Thermal metrics (temperature, slope, freq droop) are kept separate from usage metrics
so workload classification does not treat °C as utilization %.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from app.components import get_system_components
from app.models import Benchmark, BenchmarkResult, System
from app.sensor_quality import is_noisy_sensor_series, numeric_series, peak_series_value, sensor_kind
from app.workload_profile import (
    counter_signal_key,
    is_perf_counter_benchmark,
    _args_matches_config,
    _norm_text,
)

_SENSOR_KEYWORDS = (
    "temperature", "frequency", "usage", "power", "celsius", "mhz", "ghz",
    "watts", "fan", "rpm", "voltage", "energy", "utilization",
)

ML_HARDWARE_KEYS = (
    "processor",
    "graphics",
    "memory",
    "motherboard",
    "chipset",
    "cooler_model",
    "chassis_version",
    "psu",
    "custom_hardware",
    "external_off",
    "gpu_fans",
    "memory_fans",
    "nvme_fans",
    "thermal_pad_above_nvme",
    "thermal_pad_below_nvme",
    "thermal_pad_sandwich_nvme",
)


@dataclass
class ThermalSensorFeatures:
    cpu_temp_mean: float | None = None
    cpu_temp_peak: float | None = None
    cpu_temp_slope: float | None = None
    gpu_temp_mean: float | None = None
    gpu_temp_peak: float | None = None
    cpu_freq_peak: float | None = None
    cpu_freq_min: float | None = None
    gpu_freq_peak: float | None = None
    cpu_power_mean: float | None = None
    gpu_power_mean: float | None = None


@dataclass
class UsageSensorFeatures:
    cpu_usage_peak: float | None = None
    gpu_usage_peak: float | None = None


@dataclass
class SensorFeatures:
    thermal: ThermalSensorFeatures = field(default_factory=ThermalSensorFeatures)
    usage: UsageSensorFeatures = field(default_factory=UsageSensorFeatures)
    has_monitor_data: bool = False


@dataclass
class SystemRunFeatures:
    system_id: int
    title: str
    app_version: str
    config_args: str
    score_raw: float
    score_normalized: float
    run_count: int
    run_stdev: float
    run_cv: float
    perf: dict[str, float] = field(default_factory=dict)
    sensors: SensorFeatures = field(default_factory=SensorFeatures)
    hardware: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _series_slope(values: list[float]) -> float | None:
    n = len(values)
    if n < 3:
        return None
    xs = list(range(n))
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(values)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
    den = sum((x - x_mean) ** 2 for x in xs)
    if den <= 0:
        return None
    return num / den


def _label_bucket(description: str, scale: str) -> str | None:
    blob = _norm_text(description, scale)
    if "gpu" in blob or "graphics" in blob:
        return "gpu"
    if "cpu" in blob or "package" in blob or "core" in blob:
        return "cpu"
    if any(k in blob for k in ("nvme", "disk", "ssd", "storage")):
        return "storage"
    if any(k in blob for k in ("memory", "ram", "dimm")):
        return "memory"
    return None


def _proportion_is_lower_better(proportion: str | None) -> bool:
    p = (proportion or "").strip().upper()
    if p == "LIB":
        return True
    if p == "HIB":
        return False
    pl = (proportion or "").lower()
    return "lower" in pl and "better" in pl


def _collect_perf_for_system(
    title: str,
    app_version: str,
    config_args_db: str,
    system_id: int,
) -> dict[str, float]:
    perf_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(False),
    )
    if app_version:
        perf_q = perf_q.filter(Benchmark.app_version == app_version)
    out: dict[str, list[float]] = defaultdict(list)
    for bm in perf_q.all():
        if not is_perf_counter_benchmark(bm):
            continue
        key = counter_signal_key(bm)
        if not key:
            continue
        for res in BenchmarkResult.query.filter_by(benchmark_id=bm.id, system_id=system_id).all():
            if not _args_matches_config(res.arguments, config_args_db):
                continue
            if res.value is None:
                continue
            try:
                out[key].append(float(res.value))
            except (TypeError, ValueError):
                pass
    return {k: statistics.median(vs) for k, vs in out.items() if vs}


def _collect_sensors_for_system(
    title: str,
    app_version: str,
    config_args_db: str,
    system_id: int,
) -> SensorFeatures:
    sensor_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "LINE_GRAPH",
    )
    if app_version:
        sensor_q = sensor_q.filter(Benchmark.app_version == app_version)
    sensors = [
        s for s in sensor_q.all()
        if s.description and any(k in s.description.lower() for k in _SENSOR_KEYWORDS)
    ]

    thermal = ThermalSensorFeatures()
    usage = UsageSensorFeatures()
    has_data = False

    cpu_temps: list[float] = []
    cpu_temp_slopes: list[float] = []
    gpu_temps: list[float] = []
    cpu_freqs: list[float] = []
    gpu_freqs: list[float] = []
    cpu_powers: list[float] = []
    gpu_powers: list[float] = []
    cpu_usage_peaks: list[float] = []
    gpu_usage_peaks: list[float] = []

    for s_bm in sensors:
        bucket = _label_bucket(s_bm.description or "", s_bm.scale or "")
        kind = sensor_kind(s_bm.description, s_bm.scale)
        for res in BenchmarkResult.query.filter_by(benchmark_id=s_bm.id, system_id=system_id).all():
            if not _args_matches_config(res.arguments, config_args_db):
                continue
            if not res.data_json:
                continue
            if is_noisy_sensor_series(res.data_json, s_bm.description, s_bm.scale):
                continue
            nums = numeric_series(res.data_json)
            if not nums:
                continue
            has_data = True

            if kind == "usage":
                peak = peak_series_value(res.data_json)
                if peak is None:
                    continue
                if bucket == "gpu":
                    gpu_usage_peaks.append(peak)
                elif bucket == "cpu":
                    cpu_usage_peaks.append(peak)
                continue

            if kind == "frequency":
                peak = peak_series_value(res.data_json)
                lo = min(nums)
                if bucket == "gpu" and peak is not None:
                    gpu_freqs.extend([peak, lo])
                elif bucket == "cpu" and peak is not None:
                    cpu_freqs.extend([peak, lo])
                continue

            if kind == "power":
                mean_v = statistics.mean(nums)
                if bucket == "gpu":
                    gpu_powers.append(mean_v)
                elif bucket == "cpu":
                    cpu_powers.append(mean_v)
                continue

            if kind == "temperature":
                mean_v = statistics.mean(nums)
                peak_v = max(nums)
                slope = _series_slope(nums)
                if bucket == "gpu":
                    gpu_temps.extend([mean_v, peak_v])
                elif bucket == "cpu":
                    cpu_temps.extend([mean_v, peak_v])
                    if slope is not None:
                        cpu_temp_slopes.append(slope)

    def _med(xs: list[float]) -> float | None:
        return statistics.median(xs) if xs else None

    if cpu_temps:
        thermal.cpu_temp_mean = _med(cpu_temps)
        thermal.cpu_temp_peak = max(cpu_temps)
    if cpu_temp_slopes:
        thermal.cpu_temp_slope = _med(cpu_temp_slopes)
    if gpu_temps:
        thermal.gpu_temp_mean = _med(gpu_temps)
        thermal.gpu_temp_peak = max(gpu_temps)
    if cpu_freqs:
        thermal.cpu_freq_peak = max(cpu_freqs)
        thermal.cpu_freq_min = min(cpu_freqs)
    if gpu_freqs:
        thermal.gpu_freq_peak = max(gpu_freqs)
    if cpu_powers:
        thermal.cpu_power_mean = _med(cpu_powers)
    if gpu_powers:
        thermal.gpu_power_mean = _med(gpu_powers)
    if cpu_usage_peaks:
        usage.cpu_usage_peak = max(cpu_usage_peaks)
    if gpu_usage_peaks:
        usage.gpu_usage_peak = max(gpu_usage_peaks)

    return SensorFeatures(thermal=thermal, usage=usage, has_monitor_data=has_data)


def extract_system_run_features(
    system: System,
    title: str,
    app_version: str,
    config_args: str,
    *,
    primary_bm_ids: list[int] | None = None,
    is_lower_better: bool = False,
) -> SystemRunFeatures | None:
    """One row of ML features for (system, benchmark config)."""
    config_args_db = "" if (not config_args or config_args == "default") else config_args

    q = BenchmarkResult.query.filter(
        BenchmarkResult.system_id == system.id,
        BenchmarkResult.value.isnot(None),
    )
    if primary_bm_ids:
        q = q.filter(BenchmarkResult.benchmark_id.in_(primary_bm_ids))
    else:
        bm_ids = [
            b.id for b in Benchmark.query.filter(
                Benchmark.title == title,
                Benchmark.display_format == "BAR_GRAPH",
                Benchmark.is_primary.is_(True),
            ).all()
        ]
        if app_version:
            bm_ids = [
                b.id for b in Benchmark.query.filter(
                    Benchmark.title == title,
                    Benchmark.app_version == app_version,
                    Benchmark.display_format == "BAR_GRAPH",
                    Benchmark.is_primary.is_(True),
                ).all()
            ]
        q = q.filter(BenchmarkResult.benchmark_id.in_(bm_ids))
    q = q.filter(BenchmarkResult.arguments == config_args_db)

    run_vals: list[float] = []
    for res in q.all():
        if res.data_json and isinstance(res.data_json, list):
            for v in res.data_json:
                if isinstance(v, (int, float)) and math.isfinite(float(v)):
                    run_vals.append(float(v))
        elif res.value is not None:
            try:
                run_vals.append(float(res.value))
            except (TypeError, ValueError):
                pass

    if not run_vals:
        return None

    score_raw = statistics.mean(run_vals)
    y_flip = -1.0 if is_lower_better else 1.0
    run_stdev = statistics.stdev(run_vals) if len(run_vals) >= 2 else 0.0
    run_cv = (run_stdev / abs(score_raw)) if score_raw else 0.0

    hw = {k: (get_system_components(system).get(k) or "").strip() for k in ML_HARDWARE_KEYS}

    return SystemRunFeatures(
        system_id=system.id,
        title=title,
        app_version=app_version or "",
        config_args=config_args or "default",
        score_raw=score_raw,
        score_normalized=score_raw * y_flip,
        run_count=len(run_vals),
        run_stdev=run_stdev,
        run_cv=run_cv,
        perf=_collect_perf_for_system(title, app_version, config_args_db, system.id),
        sensors=_collect_sensors_for_system(title, app_version, config_args_db, system.id),
        hardware=hw,
    )


def pool_perf_signals(rows: list[SystemRunFeatures]) -> dict[str, float]:
    pooled: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for k, v in row.perf.items():
            pooled[k].append(v)
    return {k: statistics.median(vs) for k, vs in pooled.items() if vs}


def pool_sensor_features(rows: list[SystemRunFeatures]) -> dict[str, float | None]:
    """Flatten pooled sensor/thermal metrics across systems (medians)."""

    def _pool(getter) -> float | None:
        vals = [getter(r) for r in rows if getter(r) is not None]
        return statistics.median(vals) if vals else None

    return {
        "cpu_usage_peak": _pool(lambda r: r.sensors.usage.cpu_usage_peak),
        "gpu_usage_peak": _pool(lambda r: r.sensors.usage.gpu_usage_peak),
        "cpu_temp_peak": _pool(lambda r: r.sensors.thermal.cpu_temp_peak),
        "cpu_temp_slope": _pool(lambda r: r.sensors.thermal.cpu_temp_slope),
        "cpu_freq_droop": _pool(
            lambda r: (
                (r.sensors.thermal.cpu_freq_peak - r.sensors.thermal.cpu_freq_min)
                if r.sensors.thermal.cpu_freq_peak is not None
                and r.sensors.thermal.cpu_freq_min is not None
                else None
            ),
        ),
        "gpu_temp_peak": _pool(lambda r: r.sensors.thermal.gpu_temp_peak),
    }
