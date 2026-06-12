"""
Detect whether thermals explain score spread beyond hardware tier differences.
"""

from __future__ import annotations

import statistics
from typing import Any

import numpy as np
from sklearn.linear_model import LinearRegression

from app.ml.features import SystemRunFeatures

MIN_SYSTEMS_FOR_THERMAL = 3


def compute_thermal_sensitivity(rows: list[SystemRunFeatures]) -> dict[str, Any]:
    """
    Compare score residuals vs CPU temp / freq droop / cooler differences.
    """
    n = len(rows)
    if n < MIN_SYSTEMS_FOR_THERMAL:
        return {
            "available": False,
            "reason": f"need at least {MIN_SYSTEMS_FOR_THERMAL} systems (have {n})",
            "n_systems": n,
        }

    temps = [r.sensors.thermal.cpu_temp_peak for r in rows if r.sensors.thermal.cpu_temp_peak is not None]
    if len(temps) < MIN_SYSTEMS_FOR_THERMAL:
        return {
            "available": False,
            "reason": "insufficient CPU temperature MONITOR data",
            "n_systems": n,
            "n_with_temp": len(temps),
        }

    y = np.array([r.score_normalized for r in rows], dtype=float)
    y_mean = float(np.mean(y))
    residuals = y - y_mean

    temp_vec = []
    residual_vec = []
    system_notes = []
    for row, resid in zip(rows, residuals):
        t = row.sensors.thermal.cpu_temp_peak
        if t is None:
            continue
        temp_vec.append(t)
        residual_vec.append(float(resid))
        cooler = (row.hardware.get("cooler_model") or "").strip()
        system_notes.append({
            "system_id": row.system_id,
            "cpu_temp_peak": round(t, 1),
            "score_normalized": round(row.score_normalized, 4),
            "residual": round(float(resid), 4),
            "cooler_model": cooler,
        })

    if len(temp_vec) < MIN_SYSTEMS_FOR_THERMAL:
        return {
            "available": False,
            "reason": "too few systems with paired temp + score",
            "n_systems": n,
        }

    X = np.array(temp_vec, dtype=float).reshape(-1, 1)
    y_res = np.array(residual_vec, dtype=float)
    reg = LinearRegression()
    reg.fit(X, y_res)
    r2 = float(reg.score(X, y_res)) if len(y_res) > 1 else 0.0
    slope = float(reg.coef_[0])

    # LIB tests: higher temp → worse (more negative normalized score) expected if thermal matters
    sensitivity = "none"
    if abs(slope) >= 0.002 and r2 >= 0.15:
        sensitivity = "moderate"
    if abs(slope) >= 0.004 and r2 >= 0.35:
        sensitivity = "high"

    cooler_vals = {(r.hardware.get("cooler_model") or "").strip() for r in rows}
    cooler_vals.discard("")
    cooler_varies = len(cooler_vals) >= 2

    freq_droops = [
        (r.sensors.thermal.cpu_freq_peak - r.sensors.thermal.cpu_freq_min)
        for r in rows
        if r.sensors.thermal.cpu_freq_peak is not None and r.sensors.thermal.cpu_freq_min is not None
    ]
    median_droop = statistics.median(freq_droops) if freq_droops else None

    evidence = []
    if sensitivity != "none":
        direction = "lower scores at higher temps" if slope < 0 else "higher scores at higher temps"
        evidence.append(f"temp residual slope≈{slope:.4f} ({direction})")
    if median_droop is not None and median_droop >= 150:
        evidence.append(f"median CPU freq droop≈{median_droop:.0f} MHz")

    return {
        "available": True,
        "n_systems": n,
        "n_with_temp": len(temp_vec),
        "sensitivity": sensitivity,
        "temp_residual_r2": round(r2, 3),
        "temp_slope": round(slope, 5),
        "median_cpu_temp_peak": round(statistics.median(temp_vec), 1),
        "median_freq_droop_mhz": round(median_droop, 1) if median_droop is not None else None,
        "cooler_varies": cooler_varies,
        "evidence": evidence,
        "systems": system_notes,
    }
