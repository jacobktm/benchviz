"""
Detect low-signal / flat MONITOR sensor series so broad PTS exports stay readable.
"""

from __future__ import annotations

import math
import statistics
from typing import Any


MIN_SAMPLES = 3

# Minimum absolute span (max - min) by sensor class inferred from label text.
_MIN_ABS_SPAN = {
    "temperature": 0.35,   # °C — less than this over the run ≈ flat thermistor
    "frequency": 25.0,     # MHz
    "usage": 2.0,          # % or index points
    "power": 0.75,         # W
    "energy": 0.05,        # J (RAPL-style scalars plotted as lines)
    "default": 1e-6,
}

# Minimum coefficient of variation (stdev / |mean|) when mean is well above zero.
_MIN_CV = 0.002


def _norm_blob(*parts: str) -> str:
    return " ".join((p or "").strip().lower() for p in parts if p)


def sensor_kind(description: str | None, scale: str | None = None) -> str:
    blob = _norm_blob(description, scale)
    if any(k in blob for k in ("temp", "celsius", "thermal")):
        return "temperature"
    if any(k in blob for k in ("freq", "mhz", "ghz", "clock")):
        return "frequency"
    if "usage" in blob or "util" in blob:
        return "usage"
    if any(k in blob for k in ("power", "watt")):
        return "power"
    if "energy" in blob or "joule" in blob:
        return "energy"
    return "default"


def numeric_series(values: Any) -> list[float]:
    if not values:
        return []
    if isinstance(values, (int, float)):
        return [float(values)] if math.isfinite(float(values)) else []
    out: list[float] = []
    for v in values:
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            out.append(float(v))
    return out


def series_quality(
    values: Any,
    description: str = "",
    scale: str = "",
) -> dict[str, Any]:
    """Summarize a sensor time series; `is_noisy` True when it carries little information."""
    nums = numeric_series(values)
    kind = sensor_kind(description, scale)
    if len(nums) < MIN_SAMPLES:
        return {
            "n": len(nums),
            "kind": kind,
            "is_noisy": True,
            "reason": "too_few_samples",
        }

    lo, hi = min(nums), max(nums)
    span = hi - lo
    mean = statistics.mean(nums)
    stdev = statistics.stdev(nums) if len(nums) > 1 else 0.0
    abs_mean = max(abs(mean), 1e-9)
    cv = stdev / abs_mean

    min_span = _MIN_ABS_SPAN.get(kind, _MIN_ABS_SPAN["default"])

    if span <= 0:
        return {
            "n": len(nums),
            "kind": kind,
            "min": lo,
            "max": hi,
            "mean": mean,
            "span": span,
            "cv": cv,
            "is_noisy": True,
            "reason": "flat_line",
        }

    # Idle channels (common when exporting every MONITOR probe).
    if kind == "usage" and hi < 2.0:
        return {
            "n": len(nums),
            "kind": kind,
            "min": lo,
            "max": hi,
            "mean": mean,
            "span": span,
            "cv": cv,
            "is_noisy": True,
            "reason": "idle_usage",
        }
    if kind == "frequency" and hi < 50.0 and span < min_span:
        return {
            "n": len(nums),
            "kind": kind,
            "min": lo,
            "max": hi,
            "mean": mean,
            "span": span,
            "cv": cv,
            "is_noisy": True,
            "reason": "idle_frequency",
        }

    if span < min_span and cv < _MIN_CV:
        return {
            "n": len(nums),
            "kind": kind,
            "min": lo,
            "max": hi,
            "mean": mean,
            "span": span,
            "cv": cv,
            "is_noisy": True,
            "reason": "low_variation",
        }

    return {
        "n": len(nums),
        "kind": kind,
        "min": lo,
        "max": hi,
        "mean": mean,
        "span": span,
        "cv": cv,
        "is_noisy": False,
        "reason": None,
    }


def is_noisy_sensor_series(
    values: Any,
    description: str = "",
    scale: str = "",
) -> bool:
    return bool(series_quality(values, description, scale).get("is_noisy"))


def chart_has_usable_signal(
    traces: list[dict],
    description: str = "",
    scale: str = "",
) -> tuple[bool, str | None]:
    """
    True if at least one trace has a non-noisy series, or multiple traces disagree
    meaningfully (useful for cross-system compare).
    """
    if not traces:
        return False, "no_traces"

    qualities = []
    for tr in traces:
        y = tr.get("y") or tr.get("data_json") or []
        q = series_quality(y, description, scale)
        qualities.append(q)
        tr["_quality"] = q

    good = [q for q in qualities if not q.get("is_noisy")]
    if good:
        return True, None

    # All flat individually — still keep if systems diverge from each other.
    means = [q["mean"] for q in qualities if q.get("mean") is not None]
    if len(means) >= 2:
        spread = max(means) - min(means)
        kind = qualities[0].get("kind", "default")
        min_cross = _MIN_ABS_SPAN.get(kind, 0.5)
        if spread >= min_cross:
            return True, None

    reason = qualities[0].get("reason") if qualities else "unknown"
    return False, reason


def peak_series_value(values: Any) -> float | None:
    """Peak value — better than mean for usage/freq workload detection."""
    nums = numeric_series(values)
    return max(nums) if nums else None
