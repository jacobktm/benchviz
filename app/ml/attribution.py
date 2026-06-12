"""
Score attribution via regularized regression with leave-one-system-out CV.
"""

from __future__ import annotations

import statistics
from typing import Any

import numpy as np
from sklearn.linear_model import ElasticNet
from sklearn.model_selection import LeaveOneGroupOut, cross_val_score
from sklearn.preprocessing import StandardScaler

from app.ml.features import ML_HARDWARE_KEYS, SystemRunFeatures

MIN_SYSTEMS_FOR_ATTRIBUTION = 4
MIN_FEATURE_VARIATION = 2


def _encode_hardware_matrix(rows: list[SystemRunFeatures]) -> tuple[np.ndarray, list[str], list[int]]:
    """One-hot encode hardware fields that vary across the cohort."""
    varying: dict[str, set[str]] = {}
    for key in ML_HARDWARE_KEYS:
        vals = {(r.hardware.get(key) or "").strip() for r in rows}
        vals.discard("")
        if len(vals) >= MIN_FEATURE_VARIATION:
            varying[key] = vals

    feature_names: list[str] = []
    for key in sorted(varying.keys()):
        for option in sorted(varying[key]):
            feature_names.append(f"{key}={option}")

    matrix_rows: list[list[float]] = []
    groups: list[int] = []
    for row in rows:
        vec = []
        for key in sorted(varying.keys()):
            val = (row.hardware.get(key) or "").strip()
            for option in sorted(varying[key]):
                vec.append(1.0 if val == option else 0.0)
        matrix_rows.append(vec)
        groups.append(row.system_id)

    if not feature_names:
        return np.empty((len(rows), 0)), [], groups

    return np.array(matrix_rows, dtype=float), feature_names, groups


def compute_attribution(rows: list[SystemRunFeatures]) -> dict[str, Any]:
    """
    Elastic-net attribution of normalized scores to hardware cohorts.
    Uses leave-one-system-out cross-validation when enough systems exist.
    """
    n = len(rows)
    if n < MIN_SYSTEMS_FOR_ATTRIBUTION:
        return {
            "available": False,
            "reason": f"need at least {MIN_SYSTEMS_FOR_ATTRIBUTION} systems (have {n})",
            "n_systems": n,
        }

    y = np.array([r.score_normalized for r in rows], dtype=float)
    X, feature_names, groups = _encode_hardware_matrix(rows)

    if X.shape[1] == 0:
        return {
            "available": False,
            "reason": "no varying hardware features across systems",
            "n_systems": n,
        }

    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)

    model = ElasticNet(alpha=0.08, l1_ratio=0.7, max_iter=8000, random_state=42)
    logo = LeaveOneGroupOut()
    try:
        cv_scores = cross_val_score(
            model, Xs, y, cv=logo.split(Xs, y, groups=groups),
            scoring="r2",
        )
        cv_r2_mean = float(np.mean(cv_scores))
        cv_r2_std = float(np.std(cv_scores)) if len(cv_scores) > 1 else 0.0
    except Exception:
        cv_r2_mean = None
        cv_r2_std = None

    model.fit(Xs, y)
    coefs = model.coef_
    run_noise = statistics.median([r.run_cv for r in rows if r.run_cv is not None]) if rows else 0.0

    drivers = []
    for name, coef in zip(feature_names, coefs):
        if abs(coef) < 1e-6:
            continue
        drivers.append({
            "feature": name,
            "coefficient": round(float(coef), 4),
            "importance": round(abs(float(coef)), 4),
        })
    drivers.sort(key=lambda d: -d["importance"])

    total_y_var = float(np.var(y)) if len(y) > 1 else 0.0
    train_r2 = float(model.score(Xs, y)) if total_y_var > 1e-18 else 0.0
    noise_fraction = max(0.0, 1.0 - max(0.0, cv_r2_mean if cv_r2_mean is not None else train_r2))

    tier = "exploratory"
    if cv_r2_mean is not None:
        if cv_r2_mean >= 0.55:
            tier = "strong"
        elif cv_r2_mean >= 0.25:
            tier = "moderate"
        elif cv_r2_mean >= 0.0:
            tier = "weak"

    return {
        "available": True,
        "n_systems": n,
        "n_features": len(feature_names),
        "cv_r2_mean": round(cv_r2_mean, 3) if cv_r2_mean is not None else None,
        "cv_r2_std": round(cv_r2_std, 3) if cv_r2_std is not None else None,
        "train_r2": round(train_r2, 3),
        "noise_fraction": round(noise_fraction, 3),
        "median_run_cv": round(run_noise, 4),
        "confidence_tier": tier,
        "drivers": drivers[:12],
    }
