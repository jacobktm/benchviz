"""Workload characterization from LINUX_PERF counters and PTS MONITOR sensors."""

from __future__ import annotations

from typing import Any

from .. import db
from ..models import Benchmark
from ..workload_consensus import (
    MIN_ACTIVE_SHARE,
    MIN_SYSTEMS_WITH_SENSOR_EVIDENCE,
    average_score_dicts,
    classification_from_cohort_consensus,
    scope_consensus,
    score_proportions,
    signals_have_evidence,
)
from ._classification import _profile_extras, _title_scope_fallback, classify_workload
from ._constants import SCOPE_HARDWARE_KEYS, _BOTTLENECK_HARDWARE_SCOPE
from ._helpers import (
    _args_matches_config,
    _monitor_result_matches_config,
    _norm_text,
    _sensor_label,
    counter_signal_key,
    is_perf_counter_benchmark,
    option_profile_key,
)
from ._signals import _pool_signals, collect_workload_signals, collect_workload_signals_by_system

__all__ = [
    "SCOPE_HARDWARE_KEYS",
    "_args_matches_config",
    "_ml_workload_context_from_analysis",
    "_monitor_result_matches_config",
    "_normalize_insights_bottlenecks",
    "build_workload_profile",
    "counter_signal_key",
    "is_perf_counter_benchmark",
    "option_profile_key",
    "sensor_is_relevant",
    "workload_context_for_insights",
    "workload_scope_for_insights",
]


def build_workload_profile(
    title: str,
    app_version: str,
    config_args: str = "",
    system_ids: list[int] | None = None,
    description: str = "",
    *,
    option_description: str = "",
    option_scale: str = "",
) -> dict[str, Any]:
    opt_desc = (option_description or description or "").strip()
    opt_scale = (option_scale or "").strip()
    title_blob = _norm_text(title, opt_desc, opt_scale, description, config_args)
    config_key = config_args or "default"
    option_key = option_profile_key(opt_desc, opt_scale) if (opt_desc or opt_scale) else ""

    if not system_ids:
        signals = collect_workload_signals(
            title, app_version, config_args, None,
            option_description=opt_desc, option_scale=opt_scale,
        )
        classification = classify_workload(signals, title_blob)
        return {
            **classification,
            "title": title,
            "app_version": app_version or "",
            "config_args": config_key,
            "option_key": option_key,
            "option_description": opt_desc,
            "option_scale": opt_scale,
            "signals": {
                "perf": signals.get("perf") or {},
                "sensor_categories": signals.get("sensor_categories") or {},
            },
            "imputation": {
                "applied": False,
                "reason": "no_system_list",
                "n_with_evidence": 0,
                "n_imputed": 0,
                "n_total": 0,
            },
            **_profile_extras(
                classification["scope"],
                classification.get("active_bottlenecks"),
            ),
        }

    by_system = collect_workload_signals_by_system(
        title, app_version, config_args, system_ids,
        option_description=opt_desc, option_scale=opt_scale,
    )
    evidenced_ids = [sid for sid in system_ids if signals_have_evidence(by_system.get(sid))]
    missing_ids = [sid for sid in system_ids if sid not in evidenced_ids]
    n_ev = len(evidenced_ids)
    n_total = len(system_ids)

    per_system_class: dict[str, dict[str, Any]] = {}
    for sid in system_ids:
        sig = by_system.get(sid) or {"perf": {}, "sensor_categories": {}}
        per_system_class[str(sid)] = classify_workload(sig, title_blob)

    imputation: dict[str, Any] = {
        "applied": False,
        "n_with_evidence": n_ev,
        "n_imputed": len(missing_ids),
        "n_total": n_total,
        "evidenced_system_ids": evidenced_ids,
        "imputed_system_ids": missing_ids,
    }

    if n_ev < MIN_SYSTEMS_WITH_SENSOR_EVIDENCE or not missing_ids:
        imputation["reason"] = (
            "insufficient_evidence" if n_ev < 3
            else "all_have_data"
        )
        signals = _pool_signals(by_system, system_ids)
        classification = classify_workload(signals, title_blob)
    else:
        evidenced_scopes = [per_system_class[str(s)]["scope"] for s in evidenced_ids]
        avg_scores = average_score_dicts(
            [per_system_class[str(s)]["scores"] for s in evidenced_ids]
        )
        classification = classification_from_cohort_consensus(
            avg_scores,
            evidenced_scopes,
            n_ev,
            len(missing_ids),
            title_blob,
        )
        _, vote_dominant, vote_agreement = scope_consensus(evidenced_scopes)
        imputation["applied"] = True
        imputation["reason"] = (
            "cohort_multi_bottleneck"
            if len(classification.get("active_bottlenecks") or []) >= 2
            else "cohort_consensus"
        )
        imputation["scope_vote_dominant"] = vote_dominant
        imputation["scope_vote_agreement"] = round(vote_agreement, 3)
        imputation["score_proportions"] = classification.get("score_proportions")
        imputation["active_bottlenecks"] = classification.get("active_bottlenecks")
        signals = _pool_signals(by_system, evidenced_ids)
        for sid in missing_ids:
            per_system_class[str(sid)] = {
                **classification,
                "source": "imputed_from_cohort",
            }

    scope = classification["scope"]
    return {
        **classification,
        "title": title,
        "app_version": app_version or "",
        "config_args": config_key,
        "option_key": option_key,
        "option_description": opt_desc,
        "option_scale": opt_scale,
        "signals": {
            "perf": signals.get("perf") or {},
            "sensor_categories": signals.get("sensor_categories") or {},
        },
        "imputation": imputation,
        "per_system": per_system_class,
        **_profile_extras(scope, classification.get("active_bottlenecks")),
    }


def sensor_is_relevant(
    description: str | None,
    metric: str | None,
    workload: dict[str, Any] | None,
    *,
    strict: bool = True,
) -> bool:
    if not strict or not workload:
        return True
    keywords = workload.get("sensor_keywords") or []
    if not keywords:
        return True
    blob = _norm_text(description, metric)
    if not blob.strip():
        return True
    return any(k in blob for k in keywords)


def workload_scope_for_insights(
    title: str,
    app_version: str,
    config_args: str,
    analysis_json: dict | None,
    text_blob: str,
) -> str:
    return workload_context_for_insights(
        title, app_version, config_args, analysis_json, text_blob,
    )["scope"]


def _normalize_insights_bottlenecks(active: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in active or []:
        mapped = _BOTTLENECK_HARDWARE_SCOPE.get(str(raw), str(raw))
        if mapped not in seen:
            seen.add(mapped)
            out.append(mapped)
    return out


def _ml_workload_context_from_analysis(
    analysis_json: dict,
    config_args: str,
) -> dict[str, Any] | None:
    ml_root = analysis_json.get("_ml_profile")
    if not isinstance(ml_root, dict):
        return None
    key = "default" if (not config_args or config_args == "default") else config_args
    by_args = ml_root.get("by_args") or {}
    prof = by_args.get(key)
    if not isinstance(prof, dict) and len(by_args) == 1:
        prof = next(iter(by_args.values()))
    if not isinstance(prof, dict):
        return None
    wl = prof.get("workload")
    if not isinstance(wl, dict) or wl.get("insufficient_signal"):
        return None
    scope = str(wl.get("scope") or "general")
    props = wl.get("proportions") or {}
    active = _normalize_insights_bottlenecks(list(wl.get("active_bottlenecks") or []))
    if not active and scope not in ("general", "mixed") and scope:
        active = _normalize_insights_bottlenecks([scope])
    if scope == "mixed" and not active:
        active = _normalize_insights_bottlenecks([
            k for k, v in props.items()
            if k in SCOPE_HARDWARE_KEYS and v and float(v) >= MIN_ACTIVE_SHARE
        ])
    return {
        "scope": scope,
        "active_bottlenecks": active,
        "score_proportions": props,
        "source": "ml_profile",
        "confidence": wl.get("confidence"),
        "evidence": list(wl.get("evidence") or []),
    }


def workload_context_for_insights(
    title: str,
    app_version: str,
    config_args: str,
    analysis_json: dict | None,
    text_blob: str,
    *,
    option_description: str = "",
    option_scale: str = "",
) -> dict[str, Any]:
    fallback_scope = _title_scope_fallback(text_blob)
    if analysis_json:
        key = "default" if (not config_args or config_args == "default") else config_args
        ml_ctx = _ml_workload_context_from_analysis(analysis_json, key)
        if ml_ctx:
            return ml_ctx
        ok = option_profile_key(option_description, option_scale) if (option_description or option_scale) else ""
        by_option = analysis_json.get("_workload_by_option") or {}
        if ok and isinstance(by_option.get(key), dict) and by_option[key].get(ok):
            wl = by_option[key][ok]
        else:
            by_args = analysis_json.get("_workload_by_args") or {}
            wl = by_args.get(key) or analysis_json.get("_workload")
        if isinstance(wl, dict) and wl.get("scope"):
            return {
                "scope": str(wl["scope"]),
                "active_bottlenecks": _normalize_insights_bottlenecks(
                    list(wl.get("active_bottlenecks") or []),
                ),
                "score_proportions": wl.get("score_proportions") or {},
                "source": "legacy_profile",
            }
    return {
        "scope": fallback_scope,
        "active_bottlenecks": (
            [fallback_scope]
            if fallback_scope in SCOPE_HARDWARE_KEYS and fallback_scope != "general"
            else []
        ),
        "score_proportions": {},
        "source": "title_heuristic",
    }
