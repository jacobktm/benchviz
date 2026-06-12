"""
Per-hardware MONITOR sensor baselines: idle (p10) vs load (p90) ranges by model.

Lets workload/thermal logic compare "how loaded is this chip on this run"
relative to what the same CPU/GPU has shown across all benchmarks in the DB,
instead of fixed thresholds (e.g. 75°C, 65% usage).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.components import get_system_components, hardware_rank_match_key
from app.models import Benchmark, BenchmarkResult, System
from app.sensor_quality import is_noisy_sensor_series, numeric_series, peak_series_value, sensor_kind
from app.workload_profile import _norm_text

_SENSOR_KEYWORDS = (
    "temperature", "frequency", "usage", "power", "celsius", "mhz", "ghz",
    "watts", "fan", "rpm", "voltage", "energy", "utilization",
)

MIN_SAMPLES_PER_MODEL = 5
MIN_SAMPLES_GLOBAL = 3


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        raise ValueError("empty")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _label_bucket(description: str, scale: str) -> str | None:
    blob = _norm_text(description, scale)
    if "gpu" in blob or "graphics" in blob:
        return "gpu"
    if "cpu" in blob or "package" in blob or "core" in blob:
        return "cpu"
    return None


def _hardware_part_for_signal(signal_key: str) -> str | None:
    if signal_key.startswith("cpu."):
        return "processor"
    if signal_key.startswith("gpu."):
        return "graphics"
    return None


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


def extract_sensor_scalars(
    benchmark: Benchmark,
    data_json: Any,
) -> list[tuple[str, float]]:
    """
    Extract (signal_key, scalar) pairs from one MONITOR series.
    signal_key examples: cpu.usage_peak, gpu.temp_peak, cpu.freq_droop
    """
    if not data_json or not benchmark.description:
        return []
    kind = sensor_kind(benchmark.description, benchmark.scale)
    if kind != "usage" and is_noisy_sensor_series(data_json, benchmark.description, benchmark.scale):
        return []
    nums = numeric_series(data_json)
    if not nums:
        return []

    bucket = _label_bucket(benchmark.description, benchmark.scale or "")
    if bucket not in ("cpu", "gpu"):
        return []

    prefix = bucket
    out: list[tuple[str, float]] = []

    if kind == "usage":
        peak = peak_series_value(data_json)
        if peak is not None:
            out.append((f"{prefix}.usage_peak", float(peak)))
        return out

    if kind == "frequency":
        peak = peak_series_value(data_json)
        lo = min(nums)
        if peak is not None:
            out.append((f"{prefix}.freq_peak", float(peak)))
            out.append((f"{prefix}.freq_min", float(lo)))
            if prefix == "cpu" and peak > lo:
                out.append((f"{prefix}.freq_droop", float(peak - lo)))
        return out

    if kind == "power":
        out.append((f"{prefix}.power_mean", float(statistics.mean(nums))))
        return out

    if kind == "temperature":
        out.append((f"{prefix}.temp_mean", float(statistics.mean(nums))))
        out.append((f"{prefix}.temp_peak", float(max(nums))))
        if prefix == "cpu":
            slope = _series_slope(nums)
            if slope is not None:
                out.append((f"{prefix}.temp_slope", float(slope)))
        return out

    return out


@dataclass
class SensorRangeBaseline:
    hardware_part: str
    match_key: str
    signal_key: str
    n_samples: int
    idle: float
    load: float
    span: float
    median: float

    def load_fraction(self, value: float | None) -> float | None:
        """0 ≈ idle for this model, 1 ≈ typical heavy load, >1 possible outlier."""
        if value is None or not math.isfinite(value):
            return None
        if self.span <= 1e-9:
            return None
        return max(0.0, min(1.75, (float(value) - self.idle) / self.span))

    def to_dict(self) -> dict[str, Any]:
        return {
            "hardware_part": self.hardware_part,
            "match_key": self.match_key,
            "signal_key": self.signal_key,
            "n_samples": self.n_samples,
            "idle_p10": round(self.idle, 4),
            "load_p90": round(self.load, 4),
            "span": round(self.span, 4),
            "median": round(self.median, 4),
        }


@dataclass
class HardwareSensorBaselineIndex:
    """Lookup table: (hardware_part, match_key, signal_key) → idle/load range."""

    baselines: dict[tuple[str, str, str], SensorRangeBaseline] = field(default_factory=dict)
    global_keys: set[str] = field(default_factory=set)

    def lookup(self, hardware_part: str, match_key: str, signal_key: str) -> SensorRangeBaseline | None:
        part = (hardware_part or "").strip()
        mk = (match_key or "").strip()
        sk = (signal_key or "").strip()
        if not part or not sk:
            return None
        hit = self.baselines.get((part, mk, sk))
        if hit:
            return hit
        return self.baselines.get((part, "__global__", sk))

    def normalize(self, hardware_part: str, match_key: str, signal_key: str, value: float | None) -> float | None:
        base = self.lookup(hardware_part, match_key, signal_key)
        if not base:
            return None
        return base.load_fraction(value)

    def summary_for_hardware(self, hardware_part: str, match_key: str) -> list[dict[str, Any]]:
        part = (hardware_part or "").strip()
        mk = (match_key or "").strip()
        rows = [
            b.to_dict()
            for (p, m, _sk), b in self.baselines.items()
            if p == part and m == mk
        ]
        return sorted(rows, key=lambda r: r["signal_key"])

    def to_dict(self) -> dict[str, Any]:
        by_model: dict[str, list[dict]] = defaultdict(list)
        for (part, mk, _sk), b in sorted(self.baselines.items()):
            if mk == "__global__":
                continue
            label = f"{part}:{mk}"
            by_model[label].append(b.to_dict())
        return {
            "n_baselines": len(self.baselines),
            "n_models": len(by_model),
            "global_signals": sorted(self.global_keys),
            "by_model": dict(by_model),
        }


def _build_range_baseline(
    hardware_part: str,
    match_key: str,
    signal_key: str,
    values: list[float],
) -> SensorRangeBaseline | None:
    if len(values) < MIN_SAMPLES_GLOBAL:
        return None
    idle = _percentile(values, 10)
    load = _percentile(values, 90)
    span = load - idle
    if span <= 1e-9:
        span = max(abs(load) * 0.05, 1e-6)
    return SensorRangeBaseline(
        hardware_part=hardware_part,
        match_key=match_key,
        signal_key=signal_key,
        n_samples=len(values),
        idle=idle,
        load=load,
        span=span,
        median=statistics.median(values),
    )


def build_hardware_sensor_baseline_index() -> HardwareSensorBaselineIndex:
    """
    Scan all MONITOR results in the DB; build per-model idle/load ranges per signal.
    """
    sensors = Benchmark.query.filter(Benchmark.display_format == "LINE_GRAPH").all()
    sensor_by_id = {s.id: s for s in sensors if s.description}

    # (hardware_part, match_key, signal_key) -> [values]
    samples: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    system_cache: dict[int, dict[str, str]] = {}

    def _match_key(system: System, part: str) -> str:
        if system.id not in system_cache:
            comps = get_system_components(system)
            system_cache[system.id] = {
                "processor": hardware_rank_match_key("processor", comps.get("processor") or ""),
                "graphics": hardware_rank_match_key("graphics", comps.get("graphics") or ""),
            }
        return system_cache[system.id].get(part) or ""

    for s_bm in sensors:
        if not s_bm.description:
            continue
        if not any(k in s_bm.description.lower() for k in _SENSOR_KEYWORDS):
            continue
        for res in BenchmarkResult.query.filter_by(benchmark_id=s_bm.id).all():
            if not res.data_json or not res.system:
                continue
            for signal_key, val in extract_sensor_scalars(s_bm, res.data_json):
                part = _hardware_part_for_signal(signal_key)
                if not part:
                    continue
                mk = _match_key(res.system, part)
                if not mk:
                    continue
                samples[(part, mk, signal_key)].append(val)

    index = HardwareSensorBaselineIndex()
    global_pool: dict[tuple[str, str], list[float]] = defaultdict(list)

    for (part, mk, sk), vals in samples.items():
        global_pool[(part, sk)].extend(vals)
        if len(vals) >= MIN_SAMPLES_PER_MODEL:
            base = _build_range_baseline(part, mk, sk, vals)
            if base:
                index.baselines[(part, mk, sk)] = base

    for (part, sk), vals in global_pool.items():
        base = _build_range_baseline(part, "__global__", sk, vals)
        if base:
            index.baselines[(part, "__global__", sk)] = base
            index.global_keys.add(sk)

    return index


def iter_system_sensor_scalars(
    title: str,
    app_version: str,
    config_args_db: str,
    system_id: int,
) -> Iterator[tuple[str, float]]:
    """Scalars for one system + benchmark config (for normalization at read time)."""
    sensor_q = Benchmark.query.filter(Benchmark.display_format == "LINE_GRAPH")
    if title:
        sensor_q = sensor_q.filter(Benchmark.title == title)
    if app_version:
        sensor_q = sensor_q.filter(Benchmark.app_version == app_version)

    from app.workload_profile import _monitor_result_matches_config

    for s_bm in sensor_q.all():
        if not s_bm.description or not any(k in s_bm.description.lower() for k in _SENSOR_KEYWORDS):
            continue
        for res in BenchmarkResult.query.filter_by(benchmark_id=s_bm.id, system_id=system_id).all():
            if not _monitor_result_matches_config(res.arguments, config_args_db):
                continue
            for sk, val in extract_sensor_scalars(s_bm, res.data_json):
                yield sk, val
