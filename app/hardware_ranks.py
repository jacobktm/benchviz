from __future__ import annotations

from typing import Any

from app.components import hardware_rank_match_key
from app.models import HardwareTheoreticalRank


def kendall_tau_rank_correlation(x: list[float], y: list[float]) -> float | None:
    """Tau-a style Kendall: ranks as numeric positions (no tie handling)."""
    n = len(x)
    if n < 2 or len(y) != n:
        return None
    concordant = 0
    discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            prod = (x[i] - x[j]) * (y[i] - y[j])
            if prod > 0:
                concordant += 1
            elif prod < 0:
                discordant += 1
    total = concordant + discordant
    if total == 0:
        return None
    return (concordant - discordant) / total


def load_rank_lookup(part_kind: str) -> dict[str, float]:
    kind = (part_kind or "").strip().lower()
    rows = HardwareTheoreticalRank.query.filter_by(part_kind=kind).all()
    return {r.match_key: float(r.rank_value) for r in rows}


def load_spec_rank_lookup(part_kind: str) -> dict[str, float]:
    """Baseline spec scores (before empirical calibration), when present."""
    kind = (part_kind or "").strip().lower()
    rows = HardwareTheoreticalRank.query.filter_by(part_kind=kind).all()
    out: dict[str, float] = {}
    for r in rows:
        base = r.rank_value_spec
        if base is not None:
            out[r.match_key] = float(base)
        else:
            out[r.match_key] = float(r.rank_value)
    return out


def theoretical_alignment_payload(
    feature_key: str,
    cohort_rows: list[dict],
    is_lower_better: bool,
) -> dict[str, Any] | None:
    """
    Compare observed cohort performance ordering to reference hardware rank_value ordering.
    Only runs for processor / graphics when reference data exists.
    """
    fk = (feature_key or "").strip().lower()
    if fk == "processor":
        part_kind = "cpu"
    elif fk == "graphics":
        part_kind = "gpu"
    else:
        return None

    lookup = load_rank_lookup(part_kind)
    spec_lookup = load_spec_rank_lookup(part_kind)
    if not lookup:
        return {
            "available": False,
            "part_kind": part_kind,
            "reason": "No reference rows in hardware_theoretical_ranks for this part type. Run: flask sync-hardware-ranks-api — or: flask import-hardware-ranks <file.json>; then optional flask calibrate-hardware-ranks.",
        }

    enriched: list[dict[str, Any]] = []
    for c in cohort_rows:
        val = (c.get("value") or "").strip()
        mk = hardware_rank_match_key(fk, val)
        rv = lookup.get(mk)
        sv = spec_lookup.get(mk)
        enriched.append({
            "cohort_value": val,
            "match_key": mk,
            "mean_raw": c.get("mean_raw"),
            "theoretical_rank_value": rv,
            "spec_rank_value": sv,
            "matched_reference": rv is not None,
        })

    matched = [e for e in enriched if e["theoretical_rank_value"] is not None]
    n_matched = len(matched)
    n_total = len(enriched)

    out: dict[str, Any] = {
        "available": True,
        "part_kind": part_kind,
        "cohorts": enriched,
        "n_matched_reference": n_matched,
        "n_cohorts_total": n_total,
    }

    if n_matched < 2:
        out["tau"] = None
        out["summary"] = (
            "Need at least two cohorts that match your reference database to score ordering agreement."
            if n_matched < 2
            else ""
        )
        out["insufficient_matches"] = True
        return out

    def perf_score(mean: Any) -> float:
        m = float(mean)
        return -m if is_lower_better else m

    by_obs = sorted(matched, key=lambda e: perf_score(e["mean_raw"]), reverse=True)
    obs_rank = {e["cohort_value"]: i + 1 for i, e in enumerate(by_obs)}

    by_exp = sorted(matched, key=lambda e: e["theoretical_rank_value"], reverse=True)
    exp_rank = {e["cohort_value"]: i + 1 for i, e in enumerate(by_exp)}

    labels = sorted(matched, key=lambda e: e["cohort_value"])
    x = [float(exp_rank[e["cohort_value"]]) for e in labels]
    y = [float(obs_rank[e["cohort_value"]]) for e in labels]
    tau = kendall_tau_rank_correlation(x, y)
    out["tau"] = tau
    out["insufficient_matches"] = False

    if tau is None:
        out["summary"] = "Could not compute rank correlation (ties or degenerate order)."
    elif tau >= 0.66:
        out["summary"] = "Observed benchmark ordering is largely consistent with the reference hardware ranking."
    elif tau >= 0.33:
        out["summary"] = "Partial agreement between benchmark ordering and reference ranks; mixed drivers or noise."
    elif tau > -0.33:
        out["summary"] = "Weak link: benchmark spread does not follow the reference order closely for these parts."
    else:
        out["summary"] = "Ordering tends to disagree with the reference ranking—this test may be insensitive to this component or dominated by other factors."

    return out
