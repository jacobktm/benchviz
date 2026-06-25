"""Lightweight in-process analytics for BenchViz.

Provides request timing, query counting, cache hit tracking, and
diagnostic API endpoints — no external dependencies required.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict

from flask import Blueprint, jsonify
from sqlalchemy import func as sqla_func, inspect, text

from app import db

from ._hooks import register_request_hooks, current_query_count
from ._metrics import get_metrics
from app.models import Benchmark, BenchmarkAnalysis, BenchmarkResult, HardwareSpec, System

__all__ = [
    "current_query_count",
    "get_metrics",
    "register_analytics",
]

bp = Blueprint("analytics", __name__)


def register_analytics(app):
    register_request_hooks(app)
    app.register_blueprint(bp)

    # Make metrics accessible for cache tracking
    app.extensions["benchviz_metrics"] = get_metrics()


# ---------------------------------------------------------------------------
# App-performance API endpoints
# ---------------------------------------------------------------------------


@bp.route("/api/analytics/overview")
def api_overview():
    """High-level metrics: uptime, request count, avg duration, cache hits."""
    metrics = get_metrics()
    return jsonify(metrics.overview())


@bp.route("/api/analytics/endpoints")
def api_endpoints():
    """Per-endpoint request statistics."""
    metrics = get_metrics()
    return jsonify(metrics.snapshot_endpoints())


@bp.route("/api/analytics/slow")
def api_slow():
    """Recent slow requests (exceeding threshold)."""
    metrics = get_metrics()
    return jsonify(metrics.snapshot_slow())


@bp.route("/api/analytics/cache")
def api_cache():
    """Cache hit/miss statistics."""
    metrics = get_metrics()
    return jsonify({
        "ob_index": metrics.ob_cache.snapshot(),
        "signals": metrics.signals_cache.snapshot(),
    })


@bp.route("/api/analytics/health")
def api_health():
    """Basic health check."""
    healthy = True
    db_ok = True
    db_size = None
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_path = db.engine.url.database
        if db_path and os.path.isfile(db_path):
            db_size = os.path.getsize(db_path)
            wal_path = db_path + "-wal"
            if os.path.isfile(wal_path):
                db_size += os.path.getsize(wal_path)
    except Exception:
        db_ok = False
        healthy = False

    return jsonify({
        "status": "ok" if healthy else "degraded",
        "database": {
            "connectivity": "ok" if db_ok else "error",
            "size_bytes": db_size,
        },
    })


@bp.route("/api/analytics/database")
def api_database():
    """Database statistics: row counts per table, size."""
    info: dict[str, object] = {}
    try:
        inspector = inspect(db.engine)
        table_names = inspector.get_table_names()
        tables: list[dict[str, object]] = []
        for tn in sorted(table_names):
            try:
                pk_count = db.session.execute(
                    text(f"SELECT COUNT(*) FROM {tn}")
                ).scalar()
            except Exception:
                pk_count = None
            tables.append({"name": tn, "rows": pk_count})
        info["tables"] = tables
        info["table_count"] = len(tables)
    except Exception as exc:
        info["error"] = str(exc)

    db_path = db.engine.url.database
    if db_path and os.path.isfile(db_path):
        info["db_size_bytes"] = os.path.getsize(db_path)
        wal = db_path + "-wal"
        if os.path.isfile(wal):
            info["wal_size_bytes"] = os.path.getsize(wal)
        shm = db_path + "-shm"
        if os.path.isfile(shm):
            info["shm_size_bytes"] = os.path.getsize(shm)

    return jsonify(info)


# ---------------------------------------------------------------------------
# Domain (benchmark analysis) API endpoints
# ---------------------------------------------------------------------------


def _get_primary_groups() -> dict[tuple[str, str], list[Benchmark]]:
    """All primary BAR_GRAPH benchmarks grouped by (title, app_version)."""
    primary_bms = Benchmark.query.filter(
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(True),
    ).all()
    groups: dict[tuple[str, str], list[Benchmark]] = defaultdict(list)
    for bm in primary_bms:
        groups[(bm.title, bm.app_version or "")].append(bm)
    return groups


def _system_counts_by_group(title: str, app_version: str, bm_ids: list[int]) -> int:
    """Number of distinct systems with at least one result in this group."""
    return (
        db.session.query(BenchmarkResult.system_id)
        .filter(
            BenchmarkResult.benchmark_id.in_(bm_ids),
            BenchmarkResult.value.isnot(None),
        )
        .distinct()
        .count()
    )


@bp.route("/api/analytics/domain/overview")
def api_domain_overview():
    """High-level domain health: benchmark groups, coverage, gaps, quality."""
    groups = _get_primary_groups()
    ap_version_count = len(groups)

    n_systems = System.query.count()
    n_hw_specs = HardwareSpec.query.count()
    n_results = db.session.query(sqla_func.count(BenchmarkResult.id)).scalar() or 0
    n_analyses = BenchmarkAnalysis.query.count()
    n_primaries = sum(len(bms) for bms in groups.values())

    # systems per group
    n_groups_lt3 = 0
    n_groups_ge3 = 0
    n_groups_no_results = 0
    for (title, app_version), bms in groups.items():
        bm_ids = [b.id for b in bms]
        nsys = _system_counts_by_group(title, app_version, bm_ids)
        if nsys == 0:
            n_groups_no_results += 1
        elif nsys < 3:
            n_groups_lt3 += 1
        else:
            n_groups_ge3 += 1

    # Classify analysis coverage
    analysis_map: dict[tuple[str, str], dict[str, bool]] = {}
    for a in BenchmarkAnalysis.query.all():
        key = (a.benchmark_title, a.benchmark_app_version or "")
        if key in analysis_map:
            continue
        has_legacy = bool(
            a.analysis_json and any(k for k in a.analysis_json if not k.startswith("_"))
        )
        has_ml = bool(a.analysis_json and a.analysis_json.get("_ml_profile"))
        analysis_map[key] = {"legacy": has_legacy, "ml": has_ml}

    n_legacy = sum(1 for v in analysis_map.values() if v["legacy"])
    n_ml = sum(1 for v in analysis_map.values() if v["ml"])
    n_both = sum(1 for v in analysis_map.values() if v["legacy"] and v["ml"])
    n_analyzed = len(analysis_map)

    # Gaps
    sys_no_hw = n_systems - n_hw_specs
    results_no_system = (
        db.session.query(BenchmarkResult.system_id)
        .outerjoin(System, BenchmarkResult.system_id == System.id)
        .filter(System.id.is_(None))
        .count()
    )

    # Systems with zero results (orphaned)
    systems_with_results = set(
        r[0] for r in db.session.query(BenchmarkResult.system_id).distinct().all()
    )
    orphaned_systems = n_systems - len(systems_with_results)

    return jsonify({
        "benchmark_groups": {
            "total": ap_version_count,
            "primary_benchmarks": n_primaries,
            "with_no_results": n_groups_no_results,
            "with_lt_3_systems": n_groups_lt3,
            "with_ge_3_systems": n_groups_ge3,
        },
        "systems": {
            "total": n_systems,
            "with_hardware_specs": n_hw_specs,
            "without_hardware_specs": sys_no_hw,
            "orphaned_no_results": orphaned_systems,
        },
        "results": {
            "total": n_results,
            "orphaned_no_system": results_no_system,
        },
        "analyses": {
            "total_analysis_rows": n_analyses,
            "groups_with_analysis": n_analyzed,
            "with_legacy": n_legacy,
            "with_ml": n_ml,
            "with_both": n_both,
            "with_neither": max(0, ap_version_count - len(analysis_map)),
        },
    })


@bp.route("/api/analytics/domain/coverage")
def api_domain_coverage():
    """Per-benchmark-group analysis coverage details."""
    groups = _get_primary_groups()
    analysis_map: dict[tuple[str, str], dict] = {}

    for a in BenchmarkAnalysis.query.all():
        key = (a.benchmark_title, a.benchmark_app_version or "")
        if key in analysis_map:
            continue
        j = a.analysis_json or {}
        args_count = 0
        has_errors = False
        if j:
            for k, v in j.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, list):
                    args_count += 1
                    if any(isinstance(e, dict) and "error" in e for e in v):
                        has_errors = True
                elif isinstance(v, dict):
                    args_count += 1
                    if any(
                        isinstance(fv, list)
                        and any(isinstance(e, dict) and "error" in e for e in fv)
                        for fv in v.values()
                    ):
                        has_errors = True
        ml_profile = j.get("_ml_profile", {}) if isinstance(j.get("_ml_profile"), dict) else {}
        ml_args_count = len(ml_profile.get("by_args", {}))
        analysis_map[key] = {
            "has_legacy": bool(j and any(k for k in j if not k.startswith("_"))),
            "has_ml": bool(ml_profile),
            "args_analyzed": args_count,
            "ml_args_count": ml_args_count,
            "has_errors": has_errors,
        }

    rows = []
    # Pre-compute system counts per group
    group_system_counts: dict[tuple[str, str], int] = {}
    for (title, app_version), bms in groups.items():
        bm_ids = [b.id for b in bms]
        group_system_counts[(title, app_version)] = _system_counts_by_group(
            title, app_version, bm_ids
        )

    for (title, app_version), bms in sorted(groups.items()):
        nsys = group_system_counts.get((title, app_version), 0)
        n_results = (
            db.session.query(sqla_func.count(BenchmarkResult.id))
            .filter(
                BenchmarkResult.benchmark_id.in_([b.id for b in bms]),
                BenchmarkResult.value.isnot(None),
            )
            .scalar()
            or 0
        )
        info = analysis_map.get((title, app_version), {})
        rows.append({
            "title": title,
            "app_version": app_version or "",
            "n_benchmarks": len(bms),
            "n_systems": nsys,
            "n_results": n_results,
            "has_legacy": info.get("has_legacy", False),
            "has_ml": info.get("has_ml", False),
            "args_analyzed": info.get("args_analyzed", 0),
            "ml_args_analyzed": info.get("ml_args_count", 0),
            "has_errors": info.get("has_errors", False),
        })

    return jsonify({
        "total_groups": len(rows),
        "groups": rows,
    })


@bp.route("/api/analytics/domain/gaps")
def api_domain_gaps():
    """Missing data: systems without hardware specs, unanalyzed groups, etc."""
    groups = _get_primary_groups()

    # Systems missing HardwareSpec
    sys_no_hw_spec = (
        db.session.query(System)
        .outerjoin(HardwareSpec, System.id == HardwareSpec.system_id)
        .filter(HardwareSpec.id.is_(None))
        .all()
    )

    # Benchmark groups without any analysis
    analyzed_keys: set[tuple[str, str]] = set()
    for a in BenchmarkAnalysis.query.all():
        analyzed_keys.add((a.benchmark_title, a.benchmark_app_version or ""))
    group_keys_set = set(groups.keys())

    unanalyzed_keys = group_keys_set - analyzed_keys
    unanalyzed_groups: list[dict] = []
    for (title, app_version), bms in groups.items():
        if (title, app_version) not in analyzed_keys:
            bm_ids = [b.id for b in bms]
            nsys = _system_counts_by_group(title, app_version, bm_ids)
            unanalyzed_groups.append({
                "title": title,
                "app_version": app_version or "",
                "n_benchmarks": len(bms),
                "n_systems": nsys,
            })

    # Orphaned results (benchmark or system missing)
    orphaned_results = (
        db.session.query(BenchmarkResult.id, BenchmarkResult.benchmark_id, BenchmarkResult.system_id)
        .outerjoin(Benchmark, BenchmarkResult.benchmark_id == Benchmark.id)
        .outerjoin(System, BenchmarkResult.system_id == System.id)
        .filter((Benchmark.id.is_(None)) | (System.id.is_(None)))
        .count()
    )

    # Systems with zero results
    systems_with_results = set(
        r[0] for r in db.session.query(BenchmarkResult.system_id).distinct().all()
    )
    orphaned_systems: list[dict] = []
    for sys in System.query.all():
        if sys.id not in systems_with_results:
            orphaned_systems.append({
                "id": sys.id,
                "identifier": sys.identifier,
                "hardware": sys.hardware[:80] if sys.hardware else "",
            })

    return jsonify({
        "systems_without_hardware_specs": {
            "count": len(sys_no_hw_spec),
            "systems": [
                {"id": s.id, "identifier": s.identifier}
                for s in sys_no_hw_spec
            ],
        },
        "unanalyzed_groups": {
            "count": len(unanalyzed_groups),
            "groups": unanalyzed_groups,
        },
        "orphaned_results": orphaned_results,
        "orphaned_systems": {
            "count": len(orphaned_systems),
            "systems": orphaned_systems,
        },
    })


@bp.route("/api/analytics/domain/quality")
def api_domain_quality():
    """Analysis quality: ML profile stats, legacy error rates, coverage tiers."""
    groups = _get_primary_groups()
    analysis_map: dict[tuple[str, str], dict] = {}
    for a in BenchmarkAnalysis.query.all():
        key = (a.benchmark_title, a.benchmark_app_version or "")
        if key in analysis_map:
            continue
        analysis_map[key] = a

    ml_attribution_conf: list[float] = []
    ml_thermal_tiers: list[str] = []
    ml_workload_scopes: list[str] = []
    ml_platform_matched: list[bool] = []
    legacy_error_count = 0
    legacy_total_args = 0
    groups_with_legacy = 0
    legacy_has_any_feature = 0

    for key, a in analysis_map.items():
        j = a.analysis_json or {}
        has_legacy = any(k for k in j if not k.startswith("_"))
        if has_legacy:
            groups_with_legacy += 1
            for k, v in j.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, list):
                    legacy_total_args += 1
                    if any(isinstance(e, dict) and "error" in e for e in v):
                        legacy_error_count += 1
                elif isinstance(v, dict):
                    legacy_total_args += 1
                    has_non_error = False
                    for fk, fv in v.items():
                        if isinstance(fv, list) and any(
                            isinstance(e, dict) and "error" not in e for e in fv
                        ):
                            has_non_error = True
                    if not has_non_error:
                        legacy_error_count += 1
            if legacy_total_args > 0 and legacy_total_args > legacy_error_count:
                legacy_has_any_feature += 1

        ml = j.get("_ml_profile", {})
        if isinstance(ml, dict):
            by_args = ml.get("by_args", {})
            if isinstance(by_args, dict):
                for args_key, profile in by_args.items():
                    if not isinstance(profile, dict):
                        continue
                    # Attribution
                    attr = profile.get("attribution", {})
                    if isinstance(attr, dict):
                        for pk, pv in attr.items():
                            if isinstance(pv, dict) and "coef" in pv:
                                ml_attribution_conf.append(abs(pv["coef"]))
                            elif isinstance(pv, (int, float)):
                                ml_attribution_conf.append(abs(pv))
                    # Thermal
                    therm = profile.get("thermal", {})
                    if isinstance(therm, dict):
                        tier = therm.get("sensitivity_tier", therm.get("tier", ""))
                        if tier:
                            ml_thermal_tiers.append(str(tier))
                    # Workload scope
                    wl = profile.get("workload", {})
                    if isinstance(wl, dict):
                        scope = wl.get("scope", wl.get("bottleneck", ""))
                        if scope:
                            ml_workload_scopes.append(str(scope))
                        platform = wl.get("platform_match", wl.get("matched", None))
                        if platform is not None:
                            ml_platform_matched.append(bool(platform))

    # Summarize distributions
    def _tier_counts(items: list[str]) -> list[dict]:
        counts: dict[str, int] = {}
        for item in items:
            counts[item] = counts.get(item, 0) + 1
        return sorted(
            [{"value": k, "count": v} for k, v in counts.items()],
            key=lambda x: -x["count"],
        )

    return jsonify({
        "legacy_analysis": {
            "groups_with_data": groups_with_legacy,
            "groups_with_at_least_one_feature": legacy_has_any_feature,
            "args_total": legacy_total_args,
            "args_with_errors": legacy_error_count,
            "error_rate_pct": round(
                (legacy_error_count / legacy_total_args * 100) if legacy_total_args else 0,
                1,
            ),
        },
        "ml_attribution": {
            "total_samples": len(ml_attribution_conf),
            "mean_abs_coef": round(
                (sum(ml_attribution_conf) / len(ml_attribution_conf)) if ml_attribution_conf else 0,
                4,
            ),
        },
        "ml_thermal_sensitivity": {
            "total_samples": len(ml_thermal_tiers),
            "distribution": _tier_counts(ml_thermal_tiers),
        },
        "ml_workload_scope": {
            "total_samples": len(ml_workload_scopes),
            "distribution": _tier_counts(ml_workload_scopes),
        },
        "ml_platform_match": {
            "total_samples": len(ml_platform_matched),
            "matched_count": sum(ml_platform_matched),
            "unmatched_count": len(ml_platform_matched) - sum(ml_platform_matched),
        },
    })
