from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

import click
from flask import current_app

from app import db
from app.analyzer import INSIGHT_COMPONENT_KEYS, analyze_benchmarks
from app.args_pooling import (
    extract_flag_values,
    parse_args_tokens,
    parse_pool_flags,
    pool_key_for_args_by_flags,
)
from app.components import (
    get_system_components,
    hardware_rank_match_key,
)
from app.hardware_ranks_api_sync import build_rank_entries_from_api, upsert_theoretical_ranks
from app.hardware_ranks_calibrate import calibrate_hardware_ranks
from app.insights_lock import insights_rebuild_lock
from app.ml.analyzer import analyze_ml_profiles
from app.models import (
    Benchmark,
    BenchmarkAnalysis,
    BenchmarkResult,
    HardwareTheoreticalRank,
    System,
)
from app.ob_cache_sync import (
    build_ob_cache_index,
    default_ob_cache_dir,
    default_pts_clone_dir,
    sync_ob_cache,
)
from app.parser import parse_benchmark_files, parse_file
from app.repositories import SystemRepository


@click.command("nuke-db")
@click.option("--yes", is_flag=True, help="Acknowledge destructive action.")
def nuke_db(yes):
    """Drop all data and recreate tables.

    Safe-guarded: requires both BENCHVIZ_NUKE_CONFIRM=1 and --yes.
    """
    if not yes:
        click.echo(
            "ERROR: This will permanently delete ALL data. "
            "Add --yes to confirm, and set BENCHVIZ_NUKE_CONFIRM=1.",
            err=True,
        )
        raise click.Abort()

    if os.environ.get("BENCHVIZ_NUKE_CONFIRM") != "1":
        click.echo(
            "ERROR: Set BENCHVIZ_NUKE_CONFIRM=1 to confirm you want to "
            "delete every row in every table.",
            err=True,
        )
        raise click.Abort()

    with current_app.app_context():
        click.echo("Dropping all tables ...")
        db.drop_all()
        click.echo("Recreating schema ...")
        db.create_all()
        click.echo("Database reset complete — all tables are empty.")


@click.command("move-results")
@click.option("--from-system", required=True, type=int, help="Source system ID.")
@click.option("--to-system", required=True, type=int, help="Destination system ID.")
@click.option("--benchmark-title", default="", help="Only move results for this benchmark title (substring match, case-insensitive).")
@click.option("--args", "args_filter", default="", help="Only move results with this exact arguments string.")
@click.option("--dry-run", is_flag=True, help="Show what would be moved without changing anything.")
@click.option("--yes", is_flag=True, help="Confirm the operation.")
def move_results(from_system, to_system, benchmark_title, args_filter, dry_run, yes):
    """Move benchmark results from one system to another."""
    if not yes:
        click.echo("ERROR: Add --yes to confirm.", err=True)
        raise click.Abort()

    with current_app.app_context():
        src = db.session.get(System, from_system)
        dst = db.session.get(System, to_system)
        if not src:
            click.echo(f"ERROR: Source system {from_system} not found.", err=True)
            raise click.Abort()
        if not dst:
            click.echo(f"ERROR: Destination system {to_system} not found.", err=True)
            raise click.Abort()

        q = BenchmarkResult.query.filter_by(system_id=from_system)
        if benchmark_title:
            bm_ids = [
                b.id for b in Benchmark.query.filter(
                    Benchmark.title.ilike(f"%{benchmark_title}%")
                ).all()
            ]
            if not bm_ids:
                click.echo(f"No benchmarks match title substring {benchmark_title!r}.")
                return
            q = q.filter(BenchmarkResult.benchmark_id.in_(bm_ids))
        if args_filter:
            q = q.filter(BenchmarkResult.arguments == args_filter)

        rows = q.all()
        if not rows:
            click.echo("No matching results to move.")
            return

        if dry_run:
            click.echo(f"Would move {len(rows)} result(s) from system {from_system} to system {to_system}:")
            for r in rows:
                bm = db.session.get(Benchmark, r.benchmark_id)
                title = bm.title if bm else "?"
                click.echo(f"  [{r.id}] {title} args={r.arguments!r} value={r.value}")
            return

        for r in rows:
            r.system_id = to_system
        db.session.commit()
        click.echo(f"Moved {len(rows)} result(s) from system {from_system} to system {to_system}.")


@click.command("remove-results")
@click.option("--system", "system_id", required=True, type=int, help="System ID.")
@click.option("--benchmark-title", default="", help="Only remove results for this benchmark title (substring match, case-insensitive).")
@click.option("--args", "args_filter", default="", help="Only remove results with this exact arguments string.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without changing anything.")
@click.option("--yes", is_flag=True, help="Confirm the operation.")
def remove_results(system_id, benchmark_title, args_filter, dry_run, yes):
    """Delete benchmark results from a system."""
    if not yes:
        click.echo("ERROR: Add --yes to confirm.", err=True)
        raise click.Abort()

    with current_app.app_context():
        sys = db.session.get(System, system_id)
        if not sys:
            click.echo(f"ERROR: System {system_id} not found.", err=True)
            raise click.Abort()

        q = BenchmarkResult.query.filter_by(system_id=system_id)
        if benchmark_title:
            bm_ids = [
                b.id for b in Benchmark.query.filter(
                    Benchmark.title.ilike(f"%{benchmark_title}%")
                ).all()
            ]
            if not bm_ids:
                click.echo(f"No benchmarks match title substring {benchmark_title!r}.")
                return
            q = q.filter(BenchmarkResult.benchmark_id.in_(bm_ids))
        if args_filter:
            q = q.filter(BenchmarkResult.arguments == args_filter)

        rows = q.all()
        if not rows:
            click.echo("No matching results to remove.")
            return

        if dry_run:
            click.echo(f"Would remove {len(rows)} result(s) from system {system_id}:")
            for r in rows:
                bm = db.session.get(Benchmark, r.benchmark_id)
                title = bm.title if bm else "?"
                click.echo(f"  [{r.id}] {title} args={r.arguments!r} value={r.value}")
            return

        ids = [r.id for r in rows]
        BenchmarkResult.query.filter(BenchmarkResult.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        click.echo(f"Removed {len(ids)} result(s) from system {system_id}.")


@click.command("init-db")
def init_db():
    """Initialize the database."""
    with current_app.app_context():
        db.create_all()
        print("Database initialized.")


@click.command("ingest")
def ingest():
    """Ingest benchmarks from the benchmarks directory."""
    with current_app.app_context():
        project_root = os.path.dirname(current_app.root_path)
        bm_dir = os.path.join(project_root, 'benchmarks')
        if os.path.exists(bm_dir):
            parse_benchmark_files(bm_dir)
        else:
            print(f"Benchmarks directory not found at {bm_dir}")


@click.command("backfill-perf-counters")
def backfill_perf_counters():
    """Mark Linux perf counters as non-primary BAR_GRAPH metrics."""
    from sqlalchemy import func, or_

    with current_app.app_context():
        args_t = func.ltrim(func.lower(BenchmarkResult.arguments))

        perf_benchmark_ids = [
            r[0] for r in db.session.query(BenchmarkResult.benchmark_id)
            .filter(or_(args_t.like('perf %'), args_t.like('perf-%')))
            .distinct()
            .all()
        ]

        q = Benchmark.query.filter(
            Benchmark.id.in_(perf_benchmark_ids),
            Benchmark.is_primary.is_(True),
        )
        title_t = func.ltrim(func.lower(Benchmark.title))
        ident_t = func.ltrim(func.lower(Benchmark.identifier))
        desc_t = func.ltrim(func.lower(Benchmark.description))
        scale_t = func.ltrim(func.lower(Benchmark.scale))

        perf_match = or_(
            ident_t.like('perf%'),
            title_t.like('perf%'),
            desc_t.like('perf%'),
            scale_t.like('perf%'),
            func.lower(Benchmark.title).like('%perf %'),
            func.lower(Benchmark.title).like('%perf-%'),
            func.lower(Benchmark.description).like('%perf %'),
            func.lower(Benchmark.description).like('%perf-%'),
        )

        q = q.union(
            Benchmark.query.filter(
                Benchmark.is_primary.is_(True),
                perf_match
            )
        )
        rows = q.all()
        n = len(rows)
        if not n:
            print("No perf counters found to update.")
            return
        for b in rows:
            b.is_primary = False
        db.session.commit()
        print(f"Updated {n} benchmark(s): marked perf counters as non-primary.")


@click.command("rebuild-performance-insights")
@click.option("--full", is_flag=True, help="Recompute every benchmark group (not only groups with new uploads).")
def rebuild_performance_insights(full):
    """Recompute legacy Performance Insights (BenchmarkAnalysis cohort η²)."""
    with current_app.app_context():
        with insights_rebuild_lock(block=False) as acquired:
            if not acquired:
                print("Insights rebuild already in progress; skipping.")
                return
            analyze_benchmarks(incremental=not full)
        print("Legacy performance insights rebuilt.")


@click.command("rebuild-all-insights")
@click.option("--full", is_flag=True, help="Recompute every benchmark group (not only groups with new uploads).")
def rebuild_all_insights(full):
    """Recompute legacy cohort stats and ML workload/attribution/thermal profiles."""
    with current_app.app_context():
        with insights_rebuild_lock(block=False) as acquired:
            if not acquired:
                print("Insights rebuild already in progress; skipping.")
                return
            analyze_benchmarks(incremental=not full)
            n = analyze_ml_profiles(incremental=not full)
        print(f"Performance insights rebuilt (legacy + ML profiles for {n} record(s)).")


@click.command("rebuild-ml-insights")
@click.option("--full", is_flag=True, help="Recompute every benchmark group (not only groups with new uploads).")
def rebuild_ml_insights(full):
    """Recompute ML workload/attribution/thermal profiles only."""
    with current_app.app_context():
        with insights_rebuild_lock(block=False) as acquired:
            if not acquired:
                print("Insights rebuild already in progress; skipping.")
                return
            n = analyze_ml_profiles(incremental=not full)
        print(f"ML profiles updated for {n} analysis record(s).")


@click.command("debug-ml-sensors")
@click.option("--title", required=True, help="Benchmark title (exact or substring).")
@click.option("--app-version", default="", help="Benchmark app_version (optional).")
@click.option("--args", "args_value", default="default", help="Config args key (default = empty config).")
def debug_ml_sensors(title, app_version, args_value):
    """
    Print whether MONITOR/perf data links to a benchmark config for ML workload fingerprinting.
    Run on the system that hosts the database (not necessarily your dev checkout).
    """
    from sqlalchemy import func as _func
    from app.ml.analyzer import _analyze_config
    from app.ml.sensor_baselines import build_hardware_sensor_baseline_index

    title_q = (title or "").strip()
    app_ver = (app_version or "").strip()
    args_key = (args_value or "default").strip()
    if args_key == "default":
        args_key = "default"

    with current_app.app_context():
        bm_q = Benchmark.query.filter(
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary.is_(True),
        )
        if title_q:
            bm_q = bm_q.filter(_func.lower(Benchmark.title).like(f"%{title_q.lower()}%"))
        if app_ver:
            bm_q = bm_q.filter(Benchmark.app_version == app_ver)
        primaries = bm_q.all()
        if not primaries:
            print("No matching primary BAR_GRAPH benchmarks.")
            return

        rep = primaries[0]
        title_exact = rep.title
        av = rep.app_version or ""
        print(f"Benchmark: {title_exact} (v{av})")
        print(f"Primary BAR rows: {len(primaries)}")

        args_db = "" if args_key == "default" else args_key
        sensor_q = Benchmark.query.filter_by(title=title_exact, display_format="LINE_GRAPH")
        if av:
            sensor_q = sensor_q.filter_by(app_version=av)
        kw = ("temperature", "frequency", "usage", "power", "utilization")
        sensors = [
            s for s in sensor_q.all()
            if s.description and any(k in s.description.lower() for k in kw)
        ]
        print(f"MONITOR sensor definitions: {len(sensors)}")
        for s in sensors[:12]:
            n = BenchmarkResult.query.filter_by(benchmark_id=s.id).count()
            print(f"  - {s.description[:70]}  (results={n})")

        idx = build_hardware_sensor_baseline_index()
        meta = idx.to_dict()
        print(
            f"Fleet baselines: {meta.get('n_baselines', 0)} ranges, "
            f"{meta.get('n_models', 0)} hardware model(s)"
        )
        print(f"Global signals: {', '.join(meta.get('global_signals') or []) or '(none)'}")

        prof = _analyze_config(title_exact, av, args_key, primaries, baseline_index=idx)
        if not prof:
            print("No ML profile for this config (no primary results?).")
            return

        wl = prof.get("workload") or {}
        sig = prof.get("signals") or {}
        print(f"\nML workload source: {wl.get('source')}")
        print(f"Insufficient signal: {wl.get('insufficient_signal')}")
        print(f"Evidence: {wl.get('evidence')}")
        print(f"Pooled sensors: {sig.get('sensors')}")
        print(f"Normalized: {sig.get('sensors_normalized')}")
        print(f"Perf counters: {list((sig.get('perf') or {}).keys()) or '(none)'}")


@click.command("debug-insights-coverage")
@click.option("--title", default="", help="Benchmark title substring (case-insensitive). Example: 'ONNX Runtime'")
@click.option("--app-version", default="", help="Exact benchmark app_version. Example: '1.24.1'")
def debug_insights_coverage(title, app_version):
    """
    Print distinct system coverage per (benchmark title, app_version, arguments)
    for BAR_GRAPH benchmarks. Useful for verifying why Performance Insights
    may be empty.
    """
    from sqlalchemy import func as _func

    title_sub = (title or "").strip().lower()
    app_ver = (app_version or "").strip()

    with current_app.app_context():
        rows_q = (
            db.session.query(
                Benchmark.title,
                Benchmark.app_version,
                BenchmarkResult.arguments,
                _func.count(_func.distinct(BenchmarkResult.system_id)).label("n_systems"),
            )
            .join(Benchmark, Benchmark.id == BenchmarkResult.benchmark_id)
            .filter(Benchmark.display_format == "BAR_GRAPH")
            .filter(BenchmarkResult.value.isnot(None))
        )

        if title_sub:
            rows_q = rows_q.filter(_func.lower(Benchmark.title).like(f"%{title_sub}%"))
        if app_ver:
            rows_q = rows_q.filter(Benchmark.app_version == app_ver)

        rows = (
            rows_q.group_by(Benchmark.title, Benchmark.app_version, BenchmarkResult.arguments)
            .order_by(_func.count(_func.distinct(BenchmarkResult.system_id)).desc())
            .limit(25)
            .all()
        )

        if not rows:
            print("No BAR_GRAPH benchmark results found.")
            return

        print("Top BAR_GRAPH coverage rows (distinct systems per arguments):")
        for r in rows:
            arg_label = r[2] if r[2] is not None else ""
            print(f"- {r[0]} (app={r[1]}), args='{arg_label}': n_systems={r[3]}")


@click.command("debug-insights-feature-values")
@click.option("--title", required=True, help="Benchmark title substring (case-insensitive). Example: 'Timed Linux Kernel Compilation'")
@click.option("--app-version", default="", help="Exact benchmark app_version. Optional.")
@click.option("--args", "args_value", default="defconfig", help="Exact BenchmarkResult.arguments to analyze (config).")
def debug_insights_feature_values(title, app_version, args_value):
    """
    For a given benchmark (title substring + optional app-version) and exact args string,
    prints how many distinct systems have data and how many distinct values exist for each
    insight feature key.
    """
    from sqlalchemy import func as _func

    title_sub = (title or "").strip().lower()
    app_ver = (app_version or "").strip()
    args_str = (args_value or "").strip()

    with current_app.app_context():
        bm_q = Benchmark.query.filter(Benchmark.display_format == "BAR_GRAPH")
        if title_sub:
            bm_q = bm_q.filter(_func.lower(Benchmark.title).like(f"%{title_sub}%"))
        if app_ver:
            bm_q = bm_q.filter(Benchmark.app_version == app_ver)
        bms = bm_q.all()
        if not bms:
            print("No matching BAR_GRAPH benchmarks found.")
            return

        bm_ids = [b.id for b in bms]
        res_q = (
            BenchmarkResult.query
            .filter(BenchmarkResult.benchmark_id.in_(bm_ids))
            .filter(BenchmarkResult.arguments == args_str)
            .filter(BenchmarkResult.value.isnot(None))
        )
        sys_ids = sorted({r.system_id for r in res_q.all()})
        print(f"Matched benchmarks: {len(bms)}; args='{args_str}'; distinct systems with values: {len(sys_ids)}")
        if not sys_ids:
            return

        systems = SystemRepository.find_by_ids(sys_ids)
        comps = {s.id: get_system_components(s) for s in systems}

        for fk in INSIGHT_COMPONENT_KEYS:
            values_by_sys = {}
            for sid in sys_ids:
                v = (comps.get(sid, {}).get(fk) or "").strip()
                if v:
                    values_by_sys[sid] = v
            if not values_by_sys:
                print(f"- {fk}: no non-empty values extracted")
                continue
            dist = {}
            for sid, v in values_by_sys.items():
                dist.setdefault(v, set()).add(sid)
            items = sorted([(v, len(sids)) for v, sids in dist.items()], key=lambda x: -x[1])
            distinct_vals = len(items)
            print(f"- {fk}: distinct values={distinct_vals}, top={items[:3]}")


@click.command("debug-insights-analysis-features")
@click.option("--title", required=True, help="Benchmark title substring (case-insensitive), e.g. 'Timed Linux Kernel Compilation'")
@click.option("--app-version", default="", help="Exact benchmark app_version, e.g. '6.15'")
@click.option("--args", "args_value", default="defconfig", help="Exact BenchmarkResult.arguments to inspect")
def debug_insights_analysis_features(title, app_version, args_value):
    """
    Prints whether Performance Insights produced non-error features for a given benchmark/config.
    Useful for debugging why /insights shows nothing.
    """
    from sqlalchemy import func as _func

    title_sub = (title or "").strip().lower()
    app_ver = (app_version or "").strip()
    args_str = (args_value or "").strip()

    with current_app.app_context():
        q = BenchmarkAnalysis.query
        if title_sub:
            q = q.filter(_func.lower(BenchmarkAnalysis.benchmark_title).like(f"%{title_sub}%"))
        if app_ver:
            q = q.filter(BenchmarkAnalysis.benchmark_app_version == app_ver)

        rows = q.all()
        if not rows:
            print("No BenchmarkAnalysis rows found for this title/app-version.")
            return

        print("Found analysis rows:", len(rows))
        for r in rows[:5]:
            aj = r.analysis_json or {}
            feat_stats = aj.get(args_str, {}) or {}
            print(f"\n- args='{args_str}', benchmark_title='{r.benchmark_title}', app={r.benchmark_app_version}")

            ok = 0
            err = 0
            total = 0
            for feat_key, feat_vals in feat_stats.items():
                if not feat_vals:
                    continue
                total += 1
                first = feat_vals[0] if isinstance(feat_vals, list) else feat_vals
                if isinstance(first, dict) and first.get("error"):
                    err += 1
                else:
                    ok += 1

            print(f"  feature keys with data: {total}, non-error: {ok}, error: {err}")

            shown = 0
            for feat_key, feat_vals in feat_stats.items():
                if shown >= 10:
                    break
                if not feat_vals:
                    continue
                first = feat_vals[0] if isinstance(feat_vals, list) else feat_vals
                if isinstance(first, dict) and first.get("error"):
                    print(f"  [ERR] {feat_key}: {first.get('error')}")
                else:
                    name = first.get("name") if isinstance(first, dict) else None
                    n_val = first.get("n") if isinstance(first, dict) else None
                    print(f"  [OK ] {feat_key}: first='{name}' n={n_val}")
                shown += 1


@click.command("debug-insights-summary")
def debug_insights_summary():
    """
    Print a high-level summary of Performance Insights stored in BenchmarkAnalysis:
    - which DB path this process is using
    - number of analysis rows
    - how many analyses contain at least one non-error feature value (what the /insights
      template uses to decide whether to render cards vs the fallback message)
    """
    with current_app.app_context():
        print("SQLALCHEMY_DATABASE_URI:", current_app.config.get("SQLALCHEMY_DATABASE_URI"))
        analyses = BenchmarkAnalysis.query.all()
        print("BenchmarkAnalysis rows:", len(analyses))

        analyses_with_any_non_error = 0
        total_non_error_feature_entries = 0

        for r in analyses:
            aj = r.analysis_json or {}
            found_any = False
            for arg, feature_stats in aj.items():
                for feature_name, feature_values in (feature_stats or {}).items():
                    if not feature_values:
                        continue
                    first = feature_values[0] if isinstance(feature_values, list) else feature_values
                    if isinstance(first, dict) and first.get("error"):
                        continue
                    found_any = True
                    total_non_error_feature_entries += 1
            if found_any:
                analyses_with_any_non_error += 1

        print("Analyses with any non-error feature:", analyses_with_any_non_error)
        print("Total non-error feature entries (approx):", total_non_error_feature_entries)


@click.command("debug-insights-perf-args")
def debug_insights_perf_args():
    """
    Check whether Performance Insights analysis_json still contains perf-like
    BenchmarkResult.arguments keys (e.g. 'perf page-faults ...').
    """
    perf_arg_hits = defaultdict(int)
    analyses_with_perf = 0

    with current_app.app_context():
        analyses = BenchmarkAnalysis.query.all()
        for r in analyses:
            aj = r.analysis_json or {}
            found = False
            for args_key, feature_stats in aj.items():
                if not isinstance(args_key, str):
                    continue
                k = args_key.strip().lower()
                if "perf " in k or k.startswith("perf-") or k.startswith("perf "):
                    perf_arg_hits[args_key] += 1
                    found = True
            if found:
                analyses_with_perf += 1

        print("Analyses containing perf-like args keys:", analyses_with_perf)
        top = sorted(perf_arg_hits.items(), key=lambda t: t[1], reverse=True)[:20]
        for args_key, n_hits in top:
            print(f"- args='{args_key}': analyses={n_hits}")


@click.command("debug-primary-perf-benchmarks")
def debug_primary_perf_benchmarks():
    """
    Print BAR_GRAPH benchmarks marked primary that look perf-like.
    If this list is non-empty, perf counters will still appear in insights after rebuild.
    """
    with current_app.app_context():
        q = Benchmark.query.filter(
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary.is_(True),
        )

        q = q.filter(
            (Benchmark.identifier.ilike("perf%")) |
            (Benchmark.title.ilike("perf%")) |
            (Benchmark.description.ilike("perf%")) |
            (Benchmark.scale.ilike("perf%"))
        )

        rows = q.all()[:25]
        print("Primary BAR_GRAPH benchmarks that look perf-like:", len(rows))
        for b in rows:
            print(f"- id={b.id} title='{b.title}' app_version='{b.app_version}' scale='{b.scale}' identifier='{b.identifier}' desc_prefix='{(b.description or '')[:40]}'")


@click.command("import-hardware-ranks")
@click.argument("path")
def import_hardware_ranks_cmd(path):
    """
    Load CPU/GPU reference scores from JSON for theoretical-vs-observed alignment.

    Format:
      { "cpus": [ { "match_key": "AMD Ryzen 9 9950X", "rank_value": 100.0 }, ... ],
        "gpus": [ { "match_key": "NVIDIA GeForce RTX 5080", "rank_value": 95 }, ... ] }

    rank_value: higher = theoretically better. Stored as both rank_value_spec (baseline)
    and rank_value until you run `flask calibrate-hardware-ranks`.
    """
    p = Path(path)
    if not p.is_file():
        print(f"Not found: {path}")
        return

    payload = json.loads(p.read_text(encoding="utf-8"))
    counters = {"added": 0, "updated": 0}

    def ingest_list(kind_db: str, items, feature_key_for_norm: str):
        for row in items:
            if not isinstance(row, dict):
                continue
            mk_raw = (row.get("match_key") or row.get("name") or "").strip()
            if not mk_raw:
                continue
            mk = hardware_rank_match_key(feature_key_for_norm, mk_raw)
            if not mk:
                continue
            rv = row.get("rank_value")
            if rv is None:
                print(f"Skip (no rank_value): {mk_raw!r}")
                continue
            rv = float(rv)
            rec = HardwareTheoreticalRank.query.filter_by(part_kind=kind_db, match_key=mk).first()
            label = ((row.get("display_label") or mk_raw) or "")[:512] or None
            note = ((row.get("source_note") or row.get("source") or "") or "")[:255] or None
            if rec:
                rec.rank_value_spec = rv
                rec.rank_value = rv
                rec.display_label = label
                rec.source_note = note
                counters["updated"] += 1
            else:
                db.session.add(HardwareTheoreticalRank(
                    part_kind=kind_db,
                    match_key=mk,
                    rank_value=rv,
                    rank_value_spec=rv,
                    display_label=label,
                    source_note=note,
                ))
                counters["added"] += 1

    with current_app.app_context():
        cpus = payload.get("cpus") or payload.get("CPU") or []
        gpus = payload.get("gpus") or payload.get("GPU") or []
        if not cpus and not gpus:
            print("JSON must contain 'cpus' and/or 'gpus' arrays.")
            return
        ingest_list("cpu", cpus, "processor")
        ingest_list("gpu", gpus, "graphics")
        db.session.commit()
        print(
            "hardware_theoretical_ranks:",
            counters["added"], "inserted,",
            counters["updated"], "updated.",
        )


@click.command("sync-openbenchmarking-cache")
@click.option(
    "--source",
    type=click.Choice(["auto", "local", "github"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="auto: clone/update instance/phoronix-test-suite then mirror ob-cache.",
)
@click.option(
    "--local-path",
    default="",
    help="PTS source tree with ob-cache/ (default: instance/phoronix-test-suite).",
)
@click.option("--branch", default="master", show_default=True, help="Git branch when pulling from GitHub.")
@click.option(
    "--skip-clone",
    is_flag=True,
    help="Do not git clone/pull; only copy generated.json from an existing local tree.",
)
@click.option(
    "--skip-pts-run",
    is_flag=True,
    help="Do not run phoronix-test-suite (no sub-command) before copying ob-cache.",
)
@click.option(
    "--skip-live-fetch",
    is_flag=True,
    help="Do not fetch missing generated.json from OpenBenchmarking.org after git mirror.",
)
def sync_openbenchmarking_cache_cmd(
    source: str,
    local_path: str,
    branch: str,
    skip_clone: bool,
    skip_pts_run: bool,
    skip_live_fetch: bool,
):
    """
    Mirror OpenBenchmarking generated.json analytics from Phoronix Test Suite and build a lookup index.

    Order: live OB fetch for missing profiles, git ob-cache mirror, then rebuild index.
    Compare lookups use live → local → older-version fallback at runtime.
    """
    lp = local_path.strip() or None
    with current_app.app_context():
        meta = sync_ob_cache(
            source=source,
            local_path=lp,
            branch=branch,
            ensure_clone=not skip_clone,
            run_pts_update=not skip_pts_run,
            live_fetch=not skip_live_fetch,
        )
        idx = build_ob_cache_index()
        print("OpenBenchmarking cache sync:")
        print("  pts clone:", default_pts_clone_dir())
        if clone_meta := meta.get("clone"):
            print("  clone action:", clone_meta.get("action"))
            if clone_meta.get("fetch_error"):
                print("  clone fetch note:", clone_meta.get("fetch_error"))
        if pts_meta := meta.get("pts_update"):
            print("  pts update ok:", pts_meta.get("ok", pts_meta.get("skipped")))
            if pts_meta.get("reason"):
                print("  pts update note:", pts_meta.get("reason"))
        if live_meta := meta.get("live_fetch"):
            print("  live fetched:", live_meta.get("fetched"))
            print("  live refreshed (stale):", live_meta.get("refreshed_stale"))
            print("  live failed:", live_meta.get("failed"))
        print("  cache ttl hours:", os.environ.get("BENCHVIZ_OB_CACHE_TTL_HOURS", "168 (default)"))
        print("  source:", meta.get("source"))
        print("  local path:", meta.get("local_path"))
        print("  files copied:", meta.get("files_copied"))
        print("  cache dir:", default_ob_cache_dir())
        print("  index entries:", idx.get("entry_count"))
        print("  synced_at:", idx.get("synced_at"))


@click.command("sync-hardware-ranks-api")
@click.option(
    "--base-url",
    default="http://localhost:7432",
    show_default=True,
    help="Parts service root (GET /api/cpu and /api/gpu).",
)
@click.option("--timeout", default=120, show_default=True, help="HTTP timeout seconds per endpoint.")
@click.option("--dry-run", is_flag=True, help="Fetch and print counts only; do not write the database.")
def sync_hardware_ranks_api_cmd(base_url: str, timeout: int, dry_run: bool):
    """
    Pull CPUs/GPUs from your local Parts API and fill hardware_theoretical_ranks.

    Scores are derived from specs (CPU: cores × clocks × thread factor; GPU: TDP × bandwidth).
    Match keys match BenchViz processor/graphics normalization for Kendall τ alignment on Insights.
    """
    entries, errs = build_rank_entries_from_api(base_url, timeout=timeout)
    for msg in errs:
        print(msg)
    n_cpu = sum(1 for kind, *_ in entries if kind == "cpu")
    n_gpu = sum(1 for kind, *_ in entries if kind == "gpu")
    print(f"Fetched: {n_cpu} CPU keys, {n_gpu} GPU keys (after dedup).")
    if not entries:
        print("Nothing to upsert.")
        return
    if dry_run:
        print("Dry run: no database changes.")
        return
    with current_app.app_context():
        ct = upsert_theoretical_ranks(entries)
        db.session.commit()
        print(
            "hardware_theoretical_ranks:",
            ct["added"], "inserted,",
            ct["updated"], "updated.",
        )


@click.command("calibrate-hardware-ranks")
@click.option(
    "--spec-weight",
    default=0.35,
    show_default=True,
    help="Weight for spec baseline vs bench data (0=all empirical, 1=all spec).",
)
@click.option(
    "--part-kind",
    type=click.Choice(["both", "cpu", "gpu"], case_sensitive=False),
    default="both",
    show_default=True,
    help="Which part class to update.",
)
def calibrate_hardware_ranks_cmd(spec_weight: float, part_kind: str):
    """
    Recompute rank_value from rank_value_spec + primary BAR_GRAPH results in this database.

    Within each benchmark (and argument profile), systems get a performance percentile; that
    pulls 9950X3D-style parts up on cache-heavy tests when your uploads show it, even when
    the parts API scores them like a plain 9950X.
    """
    with current_app.app_context():
        out = calibrate_hardware_ranks(spec_weight=spec_weight, part_kind=part_kind.lower())
        if out.get("error"):
            print(out["error"])
            return
        db.session.commit()
        print(f"Updated {out['updated']} row(s); spec_weight={out['spec_weight']:.2f}")
        for kind, info in (out.get("detail") or {}).get("kinds", {}).items():
            print(
                f"  {kind}: {info.get('rows', 0)} rows, "
                f"{info.get('with_bench_signal', 0)} with empirical signal, "
                f"{info.get('match_keys_with_empirical', 0)} distinct parts seen in benchmarks.",
            )


@click.command("debug-pool-args")
@click.option(
    "--pool-arg-flags",
    default="--cycles-device",
    show_default=True,
    help="Flags whose values should be pooled together (comma/newline separated).",
)
@click.option(
    "--args",
    "args_list",
    multiple=True,
    required=True,
    help="Repeat this option with each args string to test.",
)
def debug_pool_args_cmd(pool_arg_flags: str, args_list: tuple[str, ...]):
    """Debug pooling argument parsing: tokenization, extracted values, pooled key."""
    pool_flags = parse_pool_flags(pool_arg_flags)
    print("pool_arg_flags raw:", pool_arg_flags)
    print("pool_flags parsed:", pool_flags)
    print("")
    for a in args_list:
        print("ARGS:", a)
        tokens = parse_args_tokens(a)
        print("  tokens:", tokens)
        extracted = extract_flag_values(a, pool_flags)
        print("  extracted values:", extracted)
        pooled = pool_key_for_args_by_flags(a, pool_flags)
        print("  pooled key:", pooled)
        print("")


@click.command("reimport-all")
@click.option("--yes", is_flag=True, help="Confirm the operation (deletes ALL existing data).")
@click.option("--benchmarks-dir", default="", help="Directory with benchmark zip files (default: ~/Documents/Benchmarks).")
def reimport_all(yes, benchmarks_dir):
    """Nuke all data, extract all benchmark zips, reimport everything fresh.

    Destroys and recreates all tables. Requires --yes.
    """
    if not yes:
        click.echo("ERROR: Add --yes to confirm. This will DELETE ALL DATA and reimport.", err=True)
        raise click.Abort()
    import glob
    import shutil
    import zipfile
    import tempfile

    bm_dir = benchmarks_dir.strip() or os.path.expanduser("~/Documents/Benchmarks")
    if not os.path.isdir(bm_dir):
        click.echo(f"ERROR: Benchmarks directory not found: {bm_dir}", err=True)
        raise click.Abort()

    with current_app.app_context():
        click.echo("Dropping all tables ...")
        db.drop_all()
        click.echo("Recreating schema ...")
        db.create_all()

        zips = sorted(glob.glob(os.path.join(bm_dir, "*.zip")))
        if not zips:
            click.echo(f"No zip files found in {bm_dir}.")
            return
        click.echo(f"Found {len(zips)} zip file(s).")

        tmp = tempfile.mkdtemp(prefix="benchviz_reimport_")
        try:
            n_imported = 0
            for zp in zips:
                name = os.path.splitext(os.path.basename(zp))[0]
                extract_dir = os.path.join(tmp, name)
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(zp, "r") as zf:
                    zf.extractall(extract_dir)
                # Find composite.xml (may be nested one level deep)
                xmls = glob.glob(os.path.join(extract_dir, "**", "composite.xml"), recursive=True)
                for xml_path in xmls:
                    click.echo(f"  Importing {xml_path} ...")
                    parse_file(xml_path)
                    n_imported += 1
            db.session.commit()
            click.echo(f"Imported {n_imported} benchmark file(s) from {len(zips)} zip(s).")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@click.command("debug-pool-axes")
@click.option(
    "--benchmark-title",
    required=True,
    help="Primary BAR_GRAPH benchmark title (e.g. Blender).",
)
@click.option(
    "--app-version",
    default="",
    show_default=True,
    help="Primary BAR_GRAPH app_version for the suite.",
)
@click.option(
    "--pool-arg-flags",
    default="--cycles-device",
    show_default=True,
    help="Flags whose values should be pooled together (comma/newline separated).",
)
@click.option(
    "--system-ids",
    required=True,
    help="Comma-separated system IDs to consider (e.g. 1,2,3).",
)
@click.option(
    "--raw-args",
    multiple=True,
    required=True,
    help="Repeat this with each selected raw args string you want to debug.",
)
def debug_pool_axes_cmd(
    benchmark_title: str,
    app_version: str,
    pool_arg_flags: str,
    system_ids: str,
    raw_args: tuple[str, ...],
):
    """Show how /api/compare pooling would group selected args into axes."""
    with current_app.app_context():
        try:
            sys_ids = [int(x.strip()) for x in system_ids.split(",") if x.strip()]
        except Exception:
            print("Invalid --system-ids (expected comma-separated ints).")
            return

        pool_flags = parse_pool_flags(pool_arg_flags)
        print("pool_arg_flags:", pool_arg_flags)
        print("pool_flags:", pool_flags)
        print("")

        raw_args_list = [str(a) for a in raw_args if a is not None]
        print("raw_args (selected):", len(raw_args_list))
        for ra in raw_args_list:
            print("  ARGS:", ra)
        print("")

        raw_args_to_value: dict[str, str] = {}
        value_order: list[str] = []
        for ra in raw_args_list:
            vals = extract_flag_values(ra, pool_flags)
            if not vals:
                continue
            v0 = str(vals[0]).strip()
            if not v0:
                continue
            raw_args_to_value[ra] = v0
            if v0 not in value_order:
                value_order.append(v0)

        print("raw_args_to_value (using first extracted value):")
        for ra, v in raw_args_to_value.items():
            print("  ", ra, "=>", v)
        print("")

        if not raw_args_to_value:
            print("No extracted pool flag values from selected raw args; nothing to pool.")
            return

        matching_primary_bm_ids = [
            bm.id
            for bm in Benchmark.query.filter(
                Benchmark.title == benchmark_title,
                Benchmark.app_version == (app_version or ""),
                Benchmark.display_format == "BAR_GRAPH",
                Benchmark.is_primary.is_(True),
            ).all()
        ]
        if not matching_primary_bm_ids:
            print("No matching primary BAR_GRAPH benchmarks found for this title/app_version.")
            return

        q_all = BenchmarkResult.query.filter(
            BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
            BenchmarkResult.system_id.in_(sys_ids),
            BenchmarkResult.arguments.in_(list(raw_args_to_value.keys())),
        ).all()

        system_present_by_value: dict[str, set[int]] = defaultdict(set)
        for r in q_all:
            v = raw_args_to_value.get(r.arguments)
            if v:
                system_present_by_value[v].add(r.system_id)

        print("system_present_by_value:")
        for v in value_order:
            print("  ", v, "=>", sorted(system_present_by_value.get(v, set())))
        print("")

        selected_sys_set = set(sys_ids)
        common_values = {v for v in value_order if system_present_by_value.get(v, set()) == selected_sys_set}
        non_common_values = [v for v in value_order if v not in common_values]
        print("common_values:", sorted(common_values))
        print("non_common_values:", non_common_values)
        print("")

        def _compatible_with_group(v: str, group_values: list[str]) -> bool:
            v_set = system_present_by_value.get(v, set())
            for m in group_values:
                m_set = system_present_by_value.get(m, set())
                if v_set.intersection(m_set):
                    return False
            return True

        axis_flag_name = pool_flags[0].lstrip("-") if pool_flags else "arg"
        seen_groups: set[frozenset[str]] = set()
        axes: list[dict[str, Any]] = []

        for ra in raw_args_list:
            v = raw_args_to_value.get(ra)
            if v and v in common_values:
                axes.append({"axis": ra, "raw_args": [ra], "values": [v], "common": True})

        for pivot in non_common_values:
            group = [pivot]
            for u in sorted(non_common_values):
                if u == pivot:
                    continue
                if _compatible_with_group(u, group):
                    group.append(u)
            gset = frozenset(group)
            if not gset or gset in seen_groups:
                continue
            seen_groups.add(gset)
            sorted_vals = sorted(gset)
            group_label = f"--{axis_flag_name} {','.join(sorted_vals)}"
            group_raw_args = [
                ra for ra in raw_args_list
                if raw_args_to_value.get(ra) in gset
            ]
            axes.append({"axis": group_label, "raw_args": group_raw_args, "values": sorted_vals, "common": False})

        print("Pooled axes that api_compare should produce:")
        for ax in axes:
            print(" -", ax["axis"], "values=", ax["values"], "raw_args=", ax["raw_args"])
