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

from app.components import get_system_components, hardware_rank_match_key
from app.models import Benchmark, BenchmarkResult, System
from app.ml.sensor_baselines import HardwareSensorBaselineIndex
from app.profile_snapshot import format_observation_label
from app.result_merge import bar_run_values, observation_batch_id
from app.sensor_quality import is_noisy_sensor_series, numeric_series, peak_series_value, sensor_kind
from app.workload_profile import (
    counter_signal_key,
    is_perf_counter_benchmark,
    _monitor_result_matches_config,
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
    """Hardware-normalized load fractions (0≈idle for this model, 1≈typical load)."""
    normalized: dict[str, float] = field(default_factory=dict)
    hardware_match_keys: dict[str, str] = field(default_factory=dict)


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
    import_batch_id: str = ""
    profile_snapshot: dict[str, Any] = field(default_factory=dict)
    observation_label: str = ""
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


def _result_matches_batch(res: BenchmarkResult, import_batch_id: str | None) -> bool:
    if not import_batch_id:
        return True
    if (res.import_batch_id or "").strip() == import_batch_id:
        return True
    if import_batch_id.startswith("legacy-"):
        try:
            return res.id == int(import_batch_id.split("-", 1)[1])
        except (ValueError, IndexError):
            return False
    return False


def _collect_perf_for_system(
    title: str,
    app_version: str,
    config_args_db: str,
    system_id: int,
    *,
    import_batch_id: str | None = None,
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
            if not _result_matches_batch(res, import_batch_id):
                continue
            if not _monitor_result_matches_config(res.arguments, config_args_db):
                continue
            if res.value is None:
                continue
            try:
                out[key].append(float(res.value))
            except (TypeError, ValueError):
                pass
    return {k: statistics.median(vs) for k, vs in out.items() if vs}


def _apply_sensor_baselines(
    sensors: SensorFeatures,
    hardware: dict[str, str],
    baseline_index: HardwareSensorBaselineIndex | None,
) -> None:
    if not baseline_index:
        return
    proc_mk = hardware_rank_match_key("processor", hardware.get("processor") or "")
    gpu_mk = hardware_rank_match_key("graphics", hardware.get("graphics") or "")
    sensors.hardware_match_keys = {
        "processor": proc_mk,
        "graphics": gpu_mk,
    }
    norm: dict[str, float] = {}

    def _set(out_key: str, part: str, mk: str, signal_key: str, raw: float | None) -> None:
        frac = baseline_index.normalize(part, mk, signal_key, raw)
        if frac is not None:
            norm[out_key] = round(frac, 3)

    t = sensors.thermal
    u = sensors.usage
    _set("cpu_usage_load_frac", "processor", proc_mk, "cpu.usage_peak", u.cpu_usage_peak)
    _set("gpu_usage_load_frac", "graphics", gpu_mk, "gpu.usage_peak", u.gpu_usage_peak)
    _set("cpu_temp_load_frac", "processor", proc_mk, "cpu.temp_peak", t.cpu_temp_peak)
    _set("gpu_temp_load_frac", "graphics", gpu_mk, "gpu.temp_peak", t.gpu_temp_peak)
    _set("cpu_power_load_frac", "processor", proc_mk, "cpu.power_mean", t.cpu_power_mean)
    _set("gpu_power_load_frac", "graphics", gpu_mk, "gpu.power_mean", t.gpu_power_mean)
    if t.cpu_freq_peak is not None:
        _set("cpu_freq_load_frac", "processor", proc_mk, "cpu.freq_peak", t.cpu_freq_peak)
    if t.gpu_freq_peak is not None:
        _set("gpu_freq_load_frac", "graphics", gpu_mk, "gpu.freq_peak", t.gpu_freq_peak)
    if t.cpu_freq_peak is not None and t.cpu_freq_min is not None:
        _set("cpu_freq_droop_frac", "processor", proc_mk, "cpu.freq_droop", t.cpu_freq_peak - t.cpu_freq_min)
    if t.cpu_temp_slope is not None:
        _set("cpu_temp_slope_frac", "processor", proc_mk, "cpu.temp_slope", t.cpu_temp_slope)

    sensors.normalized = norm


def _collect_sensors_for_system(
    title: str,
    app_version: str,
    config_args_db: str,
    system_id: int,
    *,
    hardware: dict[str, str] | None = None,
    baseline_index: HardwareSensorBaselineIndex | None = None,
    import_batch_id: str | None = None,
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
            if not _result_matches_batch(res, import_batch_id):
                continue
            if not _monitor_result_matches_config(res.arguments, config_args_db):
                continue
            if not res.data_json:
                continue
            nums = numeric_series(res.data_json)
            if not nums:
                continue

            if kind == "usage":
                # Always record usage peaks — idle GPU/CPU is meaningful workload evidence.
                peak = peak_series_value(res.data_json)
                if peak is None:
                    continue
                has_data = True
                if bucket == "gpu":
                    gpu_usage_peaks.append(peak)
                elif bucket == "cpu":
                    cpu_usage_peaks.append(peak)
                continue

            if is_noisy_sensor_series(res.data_json, s_bm.description, s_bm.scale):
                continue
            has_data = True

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

    sensors = SensorFeatures(thermal=thermal, usage=usage, has_monitor_data=has_data)
    if hardware and baseline_index:
        _apply_sensor_baselines(sensors, hardware, baseline_index)
    return sensors


def extract_system_run_features(
    system: System,
    title: str,
    app_version: str,
    config_args: str,
    *,
    primary_bm_ids: list[int] | None = None,
    is_lower_better: bool = False,
    baseline_index: HardwareSensorBaselineIndex | None = None,
    import_batch_id: str | None = None,
) -> SystemRunFeatures | None:
    """One ML feature row for (system, benchmark config, upload batch)."""
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
    profile_snapshot: dict[str, Any] = {}
    imported_at = None
    batch_key = import_batch_id or ""
    for res in q.all():
        if not _result_matches_batch(res, import_batch_id):
            continue
        run_vals.extend(bar_run_values(res.data_json, res.value))
        if res.profile_snapshot and not profile_snapshot:
            profile_snapshot = dict(res.profile_snapshot)
        if imported_at is None:
            imported_at = res.imported_at

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
        import_batch_id=batch_key,
        profile_snapshot=profile_snapshot,
        observation_label=format_observation_label(system, profile_snapshot, imported_at),
        perf=_collect_perf_for_system(
            title, app_version, config_args_db, system.id,
            import_batch_id=import_batch_id,
        ),
        sensors=_collect_sensors_for_system(
            title, app_version, config_args_db, system.id,
            hardware=hw,
            baseline_index=baseline_index,
            import_batch_id=import_batch_id,
        ),
        hardware=hw,
    )


def list_upload_observations(
    primary_bm_ids: list[int],
    config_args_db: str,
) -> list[tuple[int, str]]:
    """Distinct (system_id, import_batch_id) upload observations for one config."""
    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str]] = []
    for res in BenchmarkResult.query.filter(
        BenchmarkResult.benchmark_id.in_(primary_bm_ids),
        BenchmarkResult.arguments == config_args_db,
        BenchmarkResult.value.isnot(None),
    ).all():
        batch = (res.import_batch_id or "").strip() or observation_batch_id(res)
        key = (res.system_id, batch)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return sorted(out, key=lambda x: (x[0], x[1]))


def pool_perf_signals(rows: list[SystemRunFeatures]) -> dict[str, float]:
    pooled: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for k, v in row.perf.items():
            pooled[k].append(v)
    return {k: statistics.median(vs) for k, vs in pooled.items() if vs}


def pool_sensor_features(rows: list[SystemRunFeatures]) -> dict[str, float | None]:
    """Flatten pooled sensor metrics; includes hardware-normalized load fractions when available."""

    def _pool(getter) -> float | None:
        vals = [getter(r) for r in rows if getter(r) is not None]
        return statistics.median(vals) if vals else None

    def _pool_norm(key: str) -> float | None:
        vals = [
            r.sensors.normalized[key]
            for r in rows
            if r.sensors.normalized.get(key) is not None
        ]
        return statistics.median(vals) if vals else None

    raw = {
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
        "cpu_power_mean": _pool(lambda r: r.sensors.thermal.cpu_power_mean),
        "gpu_power_mean": _pool(lambda r: r.sensors.thermal.gpu_power_mean),
        "cpu_freq_peak": _pool(lambda r: r.sensors.thermal.cpu_freq_peak),
    }
    normalized = {
        "cpu_usage_load_frac": _pool_norm("cpu_usage_load_frac"),
        "gpu_usage_load_frac": _pool_norm("gpu_usage_load_frac"),
        "cpu_temp_load_frac": _pool_norm("cpu_temp_load_frac"),
        "gpu_temp_load_frac": _pool_norm("gpu_temp_load_frac"),
        "cpu_power_load_frac": _pool_norm("cpu_power_load_frac"),
        "gpu_power_load_frac": _pool_norm("gpu_power_load_frac"),
        "cpu_freq_load_frac": _pool_norm("cpu_freq_load_frac"),
        "gpu_freq_load_frac": _pool_norm("gpu_freq_load_frac"),
        "cpu_freq_droop_frac": _pool_norm("cpu_freq_droop_frac"),
        "cpu_temp_slope_frac": _pool_norm("cpu_temp_slope_frac"),
    }
    out = dict(raw)
    for k, v in normalized.items():
        if v is not None:
            out[k] = v
    out["has_cpu_usage"] = any(r.sensors.usage.cpu_usage_peak is not None for r in rows)
    out["has_gpu_usage"] = any(r.sensors.usage.gpu_usage_peak is not None for r in rows)
    out["has_cpu_temp"] = any(r.sensors.thermal.cpu_temp_peak is not None for r in rows)
    out["has_gpu_temp"] = any(r.sensors.thermal.gpu_temp_peak is not None for r in rows)
    out["has_cpu_power"] = any(r.sensors.thermal.cpu_power_mean is not None for r in rows)
    out["has_gpu_power"] = any(r.sensors.thermal.gpu_power_mean is not None for r in rows)
    return out
