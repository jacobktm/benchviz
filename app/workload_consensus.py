"""Pure workload cohort consensus helpers (no database access)."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

MIN_SYSTEMS_WITH_SENSOR_EVIDENCE = 2
# Kept for metadata only; imputation no longer requires a single dominant scope vote.
MIN_SCOPE_AGREEMENT = 0.6
# Minimum normalized score share to count a bottleneck as actively contributing.
MIN_ACTIVE_SHARE = 0.18

_SCORE_KEYS = ("cpu", "memory", "gpu", "storage")
_TAXONOMY_BY_SCOPE = {
    "cpu": "cpu_bound",
    "memory": "memory_bound",
    "gpu": "gpu_bound",
    "storage": "storage_bound",
    "mixed": "mixed_workload",
}


def signals_have_evidence(signals: dict[str, Any] | None) -> bool:
    if not signals:
        return False
    if signals.get("perf"):
        return True
    return bool(signals.get("sensor_categories"))


def score_proportions(scores: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(scores.get(k, 0) or 0)) for k in _SCORE_KEYS)
    if total <= 0:
        return {k: 1.0 / len(_SCORE_KEYS) for k in _SCORE_KEYS}
    return {k: max(0.0, float(scores.get(k, 0) or 0)) / total for k in _SCORE_KEYS}


def average_score_dicts(dicts: list[dict[str, float]]) -> dict[str, float]:
    if not dicts:
        return {k: 0.0 for k in _SCORE_KEYS}
    return {
        k: statistics.mean([float(d.get(k, 0) or 0) for d in dicts]) if dicts else 0.0
        for k in _SCORE_KEYS
    }


def active_bottlenecks_from_scores(
    scores: dict[str, float],
    *,
    min_share: float = MIN_ACTIVE_SHARE,
) -> list[str]:
    """Bottleneck dimensions with meaningful measured contribution (e.g. cpu + gpu both active)."""
    props = score_proportions(scores)
    active = [k for k in _SCORE_KEYS if props[k] >= min_share]
    if active:
        return sorted(active, key=lambda k: -props[k])
    peak = max((float(scores.get(k, 0) or 0) for k in _SCORE_KEYS), default=0.0)
    if peak <= 0:
        return []
    return sorted(
        [k for k in _SCORE_KEYS if float(scores.get(k, 0) or 0) >= peak * 0.4],
        key=lambda k: -float(scores.get(k, 0) or 0),
    )


def scope_consensus(scopes: list[str]) -> tuple[bool, str | None, float]:
    """Plurality vote among per-system scope labels (informational; not a hard imputation gate)."""
    if not scopes:
        return False, None, 0.0
    counts: dict[str, int] = defaultdict(int)
    for s in scopes:
        counts[s] += 1
    dominant = max(counts, key=counts.get)
    agreement = counts[dominant] / len(scopes)
    stable = len(scopes) >= MIN_SYSTEMS_WITH_SENSOR_EVIDENCE and agreement >= MIN_SCOPE_AGREEMENT
    return stable, dominant, agreement


def resolve_cohort_scope(
    avg_scores: dict[str, float],
    scope_votes: list[str] | None = None,
) -> tuple[str, str, list[str]]:
    """
    Derive cohort scope from averaged score proportions.

    A 50/50 CPU/GPU split (or any multi-active mix) is valid mixed workload — not disagreement.
    """
    props = score_proportions(avg_scores)
    active = active_bottlenecks_from_scores(avg_scores)
    if len(active) >= 2:
        return "mixed", _TAXONOMY_BY_SCOPE["mixed"], active
    if len(active) == 1:
        scope = active[0]
        return scope, _TAXONOMY_BY_SCOPE.get(scope, "mixed"), active
    if scope_votes:
        _, dominant, _ = scope_consensus(scope_votes)
        if dominant:
            return dominant, _TAXONOMY_BY_SCOPE.get(dominant, "mixed"), [dominant]
    scope = max(avg_scores, key=lambda k: float(avg_scores.get(k, 0) or 0))
    return scope, _TAXONOMY_BY_SCOPE.get(scope, "mixed"), [scope] if scope in _SCORE_KEYS else []


def classification_from_cohort_consensus(
    avg_scores: dict[str, float],
    scope_votes: list[str],
    n_with_evidence: int,
    n_imputed: int,
    title_blob: str,
) -> dict[str, Any]:
    _, vote_dominant, vote_agreement = scope_consensus(scope_votes)
    scope, taxonomy, active = resolve_cohort_scope(avg_scores, scope_votes)
    props = score_proportions(avg_scores)

    top = max(props.values()) if props else 0
    second = sorted(props.values(), reverse=True)[1] if len(props) > 1 else 0
    confidence = min(
        0.9,
        0.35
        + 0.1 * min(n_with_evidence, 4)
        + (0.06 if len(active) >= 2 and second >= top * 0.45 else 0)
        + (0.08 if top > second * 1.5 else 0),
    )
    if n_imputed:
        confidence *= 0.92

    evidence: list[str] = []
    if len(active) >= 2:
        parts = ", ".join(f"{k}≈{props[k] * 100:.0f}%" for k in active)
        evidence.append(f"cohort multi-bottleneck ({parts}, n={n_with_evidence})")
    elif vote_dominant:
        evidence.append(
            f"cohort scope={scope} ({vote_agreement * 100:.0f}% scope vote, n={n_with_evidence})",
        )
    else:
        evidence.append(f"cohort scope={scope} (n={n_with_evidence})")
    for k, share in sorted(props.items(), key=lambda x: -x[1]):
        if share >= 0.15 and k not in active:
            evidence.append(f"{k}≈{share * 100:.0f}%")

    return {
        "scope": scope,
        "taxonomy": taxonomy,
        "active_bottlenecks": active,
        "confidence": round(confidence, 3),
        "scores": {k: round(float(avg_scores.get(k, 0) or 0), 3) for k in _SCORE_KEYS},
        "score_proportions": {k: round(props[k], 3) for k in _SCORE_KEYS},
        "evidence": evidence,
        "source": "cohort_imputed",
    }
