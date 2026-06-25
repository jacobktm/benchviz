"""
Batch ML analysis: workload fingerprint, attribution, thermal sensitivity per benchmark config.
"""

from __future__ import annotations

from collections import defaultdict

from app import db
from app.ml.attribution import compute_attribution
from app.ml.features import (
    extract_system_run_features,
    pool_perf_signals,
    pool_sensor_features,
)
from app.pts import proportion_is_lower_better
from app.ml.sensor_baselines import HardwareSensorBaselineIndex, build_hardware_sensor_baseline_index
from app.ml.thermal import compute_thermal_sensitivity
from app.ml.workload import compute_workload_fingerprint
from app.models import Benchmark, BenchmarkAnalysis, BenchmarkResult
from app.insights_util import benchmark_group_needs_rebuild


def _analyze_config(
    title: str,
    app_version: str,
    config_args: str,
    primary_bms: list[Benchmark],
    *,
    baseline_index: HardwareSensorBaselineIndex | None = None,
) -> dict:
    args_key = "default" if (not config_args or config_args == "default") else config_args
    args_db = "" if args_key == "default" else config_args
    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    primary_bm_ids = [b.id for b in primary_bms]

    system_ids = sorted({
        r.system_id
        for r in BenchmarkResult.query.filter(
            BenchmarkResult.benchmark_id.in_(primary_bm_ids),
            BenchmarkResult.arguments == args_db,
            BenchmarkResult.value.isnot(None),
        ).all()
    })

    from app.models import System

    rows = []
    for sid in system_ids:
        system = db.session.get(System, sid)
        if not system:
            continue
        feat = extract_system_run_features(
            system,
            title,
            app_version,
            args_key,
            primary_bm_ids=primary_bm_ids,
            is_lower_better=is_lower_better,
            baseline_index=baseline_index,
        )
        if feat:
            rows.append(feat)

    if not rows:
        return {}

    perf = pool_perf_signals(rows)
    sensor_pool = pool_sensor_features(rows)
    rep = primary_bms[0]
    workload = compute_workload_fingerprint(
        perf,
        sensor_pool,
        title=title,
        description=(rep.description or ""),
    )
    attribution = compute_attribution(rows)
    thermal = compute_thermal_sensitivity(rows)

    hardware_baselines: dict[str, list] = {}
    if baseline_index:
        seen: set[str] = set()
        for row in rows:
            for part in ("processor", "graphics"):
                mk = row.sensors.hardware_match_keys.get(part, "")
                if not mk:
                    continue
                label = f"{part}:{mk}"
                if label in seen:
                    continue
                seen.add(label)
                summary = baseline_index.summary_for_hardware(part, mk)
                if summary:
                    hardware_baselines[label] = summary

    sensor_signals = {
        k: v for k, v in sensor_pool.items()
        if v is not None and not isinstance(v, bool)
    }
    sensor_normalized = {
        k: v for k, v in sensor_pool.items()
        if v is not None and ("_frac" in k or k.endswith("_load_frac"))
    }

    return {
        "version": 1,
        "config_args": args_key,
        "n_systems": len(rows),
        "workload": workload,
        "attribution": attribution,
        "thermal": thermal,
        "hardware_sensor_baselines": hardware_baselines,
        "signals": {
            "perf": perf,
            "sensors": sensor_signals,
            "sensors_normalized": sensor_normalized,
        },
    }


def analyze_ml_profiles(*, incremental: bool = True) -> int:
    """
    Compute ML profiles for all primary benchmark groups; merge into BenchmarkAnalysis.analysis_json.
    Returns number of analysis records updated.
    """
    mode = "incremental" if incremental else "full"
    print(f"Starting ML benchmark analysis ({mode})...")
    # Start with a fresh session so the legacy analyzer's expire_all() doesn't interfere
    db.session.remove()
    primary_bms = Benchmark.query.filter(
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(True),
    ).all()

    groups: dict[tuple[str, str], list[Benchmark]] = defaultdict(list)
    for bm in primary_bms:
        groups[(bm.title, bm.app_version or "")].append(bm)

    pending_groups = [
        ((title, app_version), bm_list)
        for (title, app_version), bm_list in groups.items()
        if benchmark_group_needs_rebuild(title, app_version, bm_list, incremental=incremental)
    ]
    if not pending_groups:
        print("No ML profile groups need rebuild.")
        return 0

    baseline_index = build_hardware_sensor_baseline_index()
    baseline_summary = baseline_index.to_dict()
    print(
        f"Built hardware sensor baselines: {baseline_summary.get('n_baselines', 0)} ranges "
        f"across {baseline_summary.get('n_models', 0)} model(s)."
    )

    updated = 0
    for (title, app_version), bm_list in pending_groups:
        rep = bm_list[0]
        all_results = []
        for bm in bm_list:
            all_results.extend(bm.results)
        if not all_results:
            continue

        args_set = sorted({
            (r.arguments or "default") if (r.arguments or "").strip() else "default"
            for r in all_results
            if r.value is not None
        })

        by_args = {}
        for args_key in args_set:
            profile = _analyze_config(
                title, app_version, args_key, bm_list,
                baseline_index=baseline_index,
            )
            if profile:
                by_args[args_key] = profile

        if not by_args:
            continue

        ml_payload = {
            "version": 1,
            "benchmark_title": title,
            "app_version": app_version,
            "by_args": by_args,
            "_hardware_sensor_baselines": baseline_summary,
        }
        if len(by_args) == 1:
            ml_payload["default"] = next(iter(by_args.values()))

        existing = BenchmarkAnalysis.query.filter_by(
            benchmark_title=title,
            benchmark_app_version=app_version,
        ).all()

        if not existing:
            record = BenchmarkAnalysis(
                benchmark_identifier=rep.identifier or "",
                benchmark_title=title,
                benchmark_app_version=app_version,
                analysis_json={"_ml_profile": ml_payload},
            )
            db.session.add(record)
            existing = [record]
        else:
            for rec in existing:
                payload = dict(rec.analysis_json or {})
                payload["_ml_profile"] = ml_payload
                rec.analysis_json = payload

        updated += len(existing)
        db.session.commit()
        db.session.expire_all()

    print(f"ML benchmark analysis complete ({updated} record(s) updated).")
    return updated
