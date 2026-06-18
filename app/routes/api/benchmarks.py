"""API endpoints for listing benchmarks and systems."""

from __future__ import annotations

from flask import request

from app import db
from app.models import Benchmark, BenchmarkResult
from app.components import get_primary_group_name
from app.repositories import BenchmarkRepository, SystemRepository
from app.route_helpers import _unique_part_of_description, format_system_profile_label

from . import bp


@bp.route('/api/common_benchmarks')
def api_common_benchmarks():
    system_ids = request.args.getlist('system_id')
    if not system_ids:
        return {"error": "Missing system_ids parameter"}, 400

    common_bms = None

    for sys_id in system_ids:
        results = BenchmarkResult.query.filter_by(system_id=sys_id).all()
        res_bm_ids = [r.benchmark_id for r in results]
        primary_bms = Benchmark.query.filter(Benchmark.id.in_(res_bm_ids), Benchmark.is_primary == True).all()

        sys_bm_keys = set()
        for bm in primary_bms:
            key = (bm.title, bm.app_version)
            sys_bm_keys.add((key, bm.id))

        if common_bms is None:
            common_bms = sys_bm_keys
        else:
            current_keys = {k[0] for k in sys_bm_keys}
            common_bms = set((k, id_val) for (k, id_val) in common_bms if k in current_keys)

    if common_bms is None:
        common_bms = set()

    try:
        sys_id_ints = [int(s) for s in system_ids]
    except (ValueError, TypeError):
        sys_id_ints = system_ids

    key_to_bm_ids = {}
    key_to_one_bm_id = {}
    for key, bm_id in common_bms:
        key_to_bm_ids.setdefault(key, set()).add(bm_id)
        if key not in key_to_one_bm_id:
            key_to_one_bm_id[key] = bm_id

    unique_common_suites = {}
    for key, bm_ids in key_to_bm_ids.items():
        config_rows = db.session.query(
            BenchmarkResult.arguments,
            BenchmarkResult.benchmark_id
        ).filter(
            BenchmarkResult.benchmark_id.in_(bm_ids),
            BenchmarkResult.system_id.in_(sys_id_ints)
        ).distinct().all()
        args_to_bm_id = {}
        for r in config_rows:
            a = r[0] if r[0] is not None else ""
            if a not in args_to_bm_id:
                args_to_bm_id[a] = r[1]
        if not args_to_bm_id:
            unique_common_suites[key] = {
                'id': key_to_one_bm_id[key],
                'label': f"{key[0]} ({key[1]})",
                'configs': []
            }
            continue
        bm_ids_for_desc = list(set(args_to_bm_id.values()))
        benchmarks_by_id = {bm.id: bm for bm in Benchmark.query.filter(Benchmark.id.in_(bm_ids_for_desc)).all()}
        args_to_description = {}
        for a, bid in args_to_bm_id.items():
            bm = benchmarks_by_id.get(bid)
            if bm and bm.description:
                args_to_description[a] = bm.description
        config_values = list(args_to_bm_id.keys())
        configs = []
        for i, a in enumerate(config_values):
            desc = args_to_description.get(a) or ""
            if (a or "").strip():
                label = a
            else:
                other_descriptions = [args_to_description.get(x) or "" for x in config_values if (x or "").strip()]
                label = _unique_part_of_description(desc, other_descriptions) or ("Option " + str(i + 1))
            configs.append({"value": a, "label": label or a or ("Option " + str(i + 1))})
        unique_common_suites[key] = {
            'id': key_to_one_bm_id[key],
            'label': f"{key[0]} ({key[1]})",
            'configs': configs
        }

    output_list = sorted(list(unique_common_suites.values()), key=lambda x: x['label'])
    return {"benchmarks": output_list}


@bp.route('/api/systems_for_benchmark')
def api_systems_for_benchmark():
    benchmark_id = request.args.get('benchmark_id')
    args_filter = request.args.get('args')
    if not benchmark_id:
        return {"error": "Missing benchmark_id parameter"}, 400

    try:
        benchmark_id = int(benchmark_id)
    except (ValueError, TypeError):
        return {"error": "Invalid benchmark_id"}, 400

    q = BenchmarkResult.query.filter_by(benchmark_id=benchmark_id)
    if args_filter is not None and args_filter != "":
        q = q.filter(BenchmarkResult.arguments == args_filter)
    results = q.all()
    sys_ids = list(set(r.system_id for r in results))

    systems = SystemRepository.find_by_ids(sys_ids)

    return {
        "systems": [
            {
                "id": s.id,
                "identifier": s.identifier,
                "chassis_version": s.chassis_version,
                "primary_system_name": get_primary_group_name(s),
                "label": format_system_profile_label(s)
            } for s in systems
        ]
    }


@bp.route('/api/systems_for_benchmark_title')
def api_systems_for_benchmark_title():
    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400

    args_db = ''
    if args_str and args_str.lower() != 'default':
        args_db = args_str

    primary_bms = BenchmarkRepository.find_primary_by_title(title, app_version)
    if not primary_bms:
        return {"systems": []}

    primary_bm_ids = [b.id for b in primary_bms]

    results_q = (
        BenchmarkResult.query
        .filter(
            BenchmarkResult.benchmark_id.in_(primary_bm_ids),
            BenchmarkResult.arguments == args_db,
            BenchmarkResult.value.isnot(None),
        )
    )
    results = results_q.all()
    sys_ids = list({r.system_id for r in results})
    if not sys_ids:
        return {"systems": []}

    systems = SystemRepository.find_by_ids(sys_ids)
    return {
        "systems": [
            {
                "id": s.id,
                "identifier": s.identifier,
                "chassis_version": s.chassis_version,
                "primary_system_name": get_primary_group_name(s),
                "label": format_system_profile_label(s)
            } for s in systems
        ]
    }, 200
