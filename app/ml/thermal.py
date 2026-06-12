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

    temps = [
        r.sensors.normalized.get("cpu_temp_load_frac")
        for r in rows
        if r.sensors.normalized.get("cpu_temp_load_frac") is not None
    ]
    uses_normalized = len(temps) >= MIN_SYSTEMS_FOR_THERMAL

    if not uses_normalized:
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
        if uses_normalized:
            t = row.sensors.normalized.get("cpu_temp_load_frac")
        else:
            t = row.sensors.thermal.cpu_temp_peak
        if t is None:
            continue
        temp_vec.append(t)
        residual_vec.append(float(resid))
        cooler = (row.hardware.get("cooler_model") or "").strip()
        note = {
            "system_id": row.system_id,
            "score_normalized": round(row.score_normalized, 4),
            "residual": round(float(resid), 4),
            "cooler_model": cooler,
        }
        if uses_normalized:
            note["cpu_temp_load_frac"] = round(t, 3)
            if row.sensors.thermal.cpu_temp_peak is not None:
                note["cpu_temp_peak_c"] = round(row.sensors.thermal.cpu_temp_peak, 1)
        else:
            note["cpu_temp_peak"] = round(t, 1)
        system_notes.append(note)

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

    freq_droops = []
    for r in rows:
        droop_frac = r.sensors.normalized.get("cpu_freq_droop_frac")
        if droop_frac is not None:
            freq_droops.append(droop_frac)
            continue
        if r.sensors.thermal.cpu_freq_peak is not None and r.sensors.thermal.cpu_freq_min is not None:
            freq_droops.append(r.sensors.thermal.cpu_freq_peak - r.sensors.thermal.cpu_freq_min)
    median_droop = statistics.median(freq_droops) if freq_droops else None
    droop_is_normalized = any(r.sensors.normalized.get("cpu_freq_droop_frac") is not None for r in rows)

    evidence = []
    if sensitivity != "none":
        axis = "thermal load" if uses_normalized else "temp"
        direction = f"lower scores at higher {axis}" if slope < 0 else f"higher scores at higher {axis}"
        evidence.append(f"temp residual slope≈{slope:.4f} ({direction})")
    if median_droop is not None:
        if droop_is_normalized and median_droop >= 0.55:
            evidence.append(f"median CPU freq droop≈{median_droop * 100:.0f}% of model span")
        elif not droop_is_normalized and median_droop >= 150:
            evidence.append(f"median CPU freq droop≈{median_droop:.0f} MHz")

    return {
        "available": True,
        "n_systems": n,
        "n_with_temp": len(temp_vec),
        "uses_hardware_normalized_temps": uses_normalized,
        "sensitivity": sensitivity,
        "temp_residual_r2": round(r2, 3),
        "temp_slope": round(slope, 5),
        "median_cpu_temp_peak": round(statistics.median(temp_vec), 3 if uses_normalized else 1),
        "median_freq_droop_mhz": round(median_droop, 1) if median_droop is not None and not droop_is_normalized else None,
        "median_cpu_freq_droop_frac": round(median_droop, 3) if median_droop is not None and droop_is_normalized else None,
        "cooler_varies": cooler_varies,
        "evidence": evidence,
        "systems": system_notes,
    }
