"""API endpoints for compare, save/saved comparison, and pool flag suggestions."""

from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict

from flask import jsonify, request, url_for
from urllib.parse import unquote

from app import db
from app.models import Benchmark, BenchmarkResult, SavedComparison, System
from app.pts import proportion_is_lower_better
from app.repositories import BenchmarkRepository, SystemRepository
from app.result_merge import bar_run_values
from app.profile_snapshot import format_observation_label
from app.route_helpers import (
    _unique_part_of_description,
    format_system_profile_label,
    generate_comparison_id,
    serialize_compare_system_groups,
)

from . import bp


@bp.route('/api/save_comparison', methods=['POST'])
def api_save_comparison():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return {"error": "Invalid JSON payload"}, 400

    if not isinstance(payload, dict):
        return {"error": "Payload must be an object"}, 400

    systems = payload.get('systems') or []
    benchmarks = payload.get('benchmarks') or []
    if not systems or not benchmarks:
        return {"error": "Payload must include non-empty 'systems' and 'benchmarks' arrays"}, 400

    comp_id = generate_comparison_id()
    saved = SavedComparison(id=comp_id, payload_json=payload)
    db.session.add(saved)
    db.session.commit()
    return {"id": comp_id}, 200


@bp.route('/api/saved_comparison/<string:comp_id>')
def api_saved_comparison(comp_id):
    saved = SavedComparison.query.get(comp_id)
    if not saved:
        return {"error": "Comparison not found"}, 404
    return saved.payload_json, 200


_timings: list[tuple[str, float]] = []

def _t(label: str) -> None:
    _timings.append((label, time.perf_counter()))

_PROFILE_PATH = "/tmp/benchviz_last_compare_profile.json"

def _save_profile(meta: dict) -> None:
    timings_dict = {}
    for i in range(1, len(_timings)):
        label = _timings[i][0]
        secs = round(_timings[i][1] - _timings[i-1][1], 3)
        timings_dict[label] = secs
    data = {"timings": timings_dict, "meta": meta, "raw_timings": _timings.copy()}
    try:
        with open(_PROFILE_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


@bp.route('/api/compare')
def api_compare():
    _timings.clear()
    _t("start")
    system_ids = request.args.getlist('system_ids')
    config_params = request.args.getlist('config')
    benchmark_ids = request.args.getlist('benchmark_id')

    _dbg: list[dict] = []
    def _dbg_append(**kw: object) -> None:
        _dbg.append(kw)

    if not system_ids:
        return {"error": "Missing system_ids parameter(s)"}, 400

    config_list = []
    if config_params:
        for c in config_params:
            part = (c or "").strip()
            if "|" in part:
                b_id_str, args_str = part.split("|", 1)
                args_str = (args_str.strip() or None)
                if args_str:
                    try:
                        args_str = unquote(args_str)
                    except Exception:
                        pass
                config_list.append((b_id_str, args_str))
            else:
                config_list.append((part.strip(), None))
    elif benchmark_ids:
        for b_id in benchmark_ids:
            config_list.append((b_id, None))

    if not config_list:
        return {"error": "Missing benchmark_id or config parameter(s)"}, 400

    try:
        sys_id_ints = [int(s) for s in system_ids]
    except (ValueError, TypeError):
        sys_id_ints = []
    if not sys_id_ints:
        return {"error": "Invalid system_ids"}, 400

    pool_equivalent_configs = str(request.args.get('pool_equivalent_configs') or '').strip().lower() in {
        '1', 'true', 'yes', 'on'
    }
    pool_resolution_classes = str(request.args.get('pool_resolution_classes') or '').strip().lower() in {
        '1', 'true', 'yes', 'on'
    }
    from app.args_pooling import (
        extract_flag_values,
        parse_pool_flags,
        pool_key_for_args_by_flags,
    )

    pool_flags = parse_pool_flags(request.args.get('pool_arg_flags'))
    if pool_equivalent_configs and not pool_flags:
        pool_equivalent_configs = False

    comparison_groups = []
    from app.ob_cache_sync import load_ob_cache_index
    from app.pts import (
        build_pts_context_for_compare_group,
        build_pts_global_harmonic_summary,
        build_pts_global_summary,
        build_pts_ob_global_summary,
    )

    ob_index_cache = load_ob_cache_index()
    _t("ob_index_loaded")

    systems_list = System.query.filter(System.id.in_(sys_id_ints)).all()
    _t("systems_loaded")
    systems_by_id = {s.id: s for s in systems_list}
    _dbg_append(step="systems", systems=[{"id": s.id, "identifier": s.identifier} for s in systems_list])
    pool_raw_args_map = defaultdict(set)
    if pool_equivalent_configs:
        for b_id, args_filter in config_list:
            try:
                b_id_int = int(b_id)
            except (ValueError, TypeError):
                continue
            if args_filter is None:
                continue
            primary_benchmark = db.session.get(Benchmark, b_id_int)
            if not primary_benchmark:
                continue
            if not getattr(primary_benchmark, "is_primary", False):
                candidate = BenchmarkRepository.find_first_primary(primary_benchmark.title, primary_benchmark.app_version)
                if candidate:
                    primary_benchmark = candidate
            pk = pool_key_for_args_by_flags(args_filter, pool_flags)
            if pk:
                pool_raw_args_map[(primary_benchmark.title, primary_benchmark.app_version, pk)].add(args_filter)

    pool_processed_keys = set()

    def _pooled_flag_suffix_from_args(args_text: str | None) -> str:
        if not args_text or not pool_flags:
            return ""
        vals = extract_flag_values(args_text, pool_flags)
        if not vals:
            return ""
        if len(vals) == 1:
            return str(vals[0])
        return "/".join(str(v) for v in vals[:3])

    for b_id, args_filter in config_list:
        try:
            b_id = int(b_id)
        except (ValueError, TypeError):
            continue
        primary_benchmark = db.session.get(Benchmark, b_id)
        if not primary_benchmark:
            continue
        if not getattr(primary_benchmark, "is_primary", False):
            candidate = BenchmarkRepository.find_first_primary(primary_benchmark.title, primary_benchmark.app_version)
            if candidate:
                primary_benchmark = candidate

        ids_with_results = [
            r[0] for r in db.session.query(BenchmarkResult.benchmark_id)
            .filter(BenchmarkResult.system_id.in_(sys_id_ints))
            .distinct().all()
        ]
        matching_primary_bm_ids = [
            bm.id for bm in Benchmark.query.filter(
                Benchmark.id.in_(ids_with_results),
                Benchmark.title == primary_benchmark.title,
                Benchmark.app_version == primary_benchmark.app_version,
                Benchmark.display_format == 'BAR_GRAPH',
                Benchmark.is_primary == True,
            ).all()
        ]
        if not matching_primary_bm_ids:
            matching_primary_bm_ids = [primary_benchmark.id]

        pooling_active = False
        raw_args_for_query_by_args_val = None
        resolution_raw_map: dict[str, list[str]] | None = None
        resolution_class_name: str | None = None

        if pool_equivalent_configs and args_filter is not None:
            current_base_key = pool_key_for_args_by_flags(args_filter, pool_flags) or str(args_filter)
            suite_key = (primary_benchmark.title, primary_benchmark.app_version)
            suite_task_key = (suite_key[0], suite_key[1], "pool-axes", current_base_key)
            if suite_task_key in pool_processed_keys:
                continue
            pool_processed_keys.add(suite_task_key)
            pooling_active = True

            suite_raw_args_filters: list[str] = []
            for b_id2, args_filter2 in config_list:
                if args_filter2 is None:
                    continue
                try:
                    b_id2_int = int(b_id2)
                except (ValueError, TypeError):
                    continue
                b2 = db.session.get(Benchmark, b_id2_int)
                if not b2:
                    continue
                if not getattr(b2, "is_primary", False):
                    cand = BenchmarkRepository.find_first_primary(b2.title, b2.app_version)
                    if cand:
                        b2 = cand
                if getattr(b2, "title", None) == primary_benchmark.title and (b2.app_version or "") == (primary_benchmark.app_version or ""):
                    suite_raw_args_filters.append(str(args_filter2))

            deduped = []
            seen_ra = set()
            for ra in suite_raw_args_filters:
                if ra in seen_ra:
                    continue
                seen_ra.add(ra)
                deduped.append(ra)
            suite_raw_args_filters = deduped

            suite_raw_args_filters = [
                ra for ra in suite_raw_args_filters
                if (pool_key_for_args_by_flags(ra, pool_flags) or ra) == current_base_key
            ]

            raw_args_to_value: dict[str, str] = {}
            value_order: list[str] = []
            for ra in suite_raw_args_filters:
                vals = extract_flag_values(ra, pool_flags)
                if not vals:
                    continue
                v0 = str(vals[0]).strip()
                if not v0:
                    continue
                raw_args_to_value[ra] = v0
                if v0 not in value_order:
                    value_order.append(v0)

            if not raw_args_to_value:
                pooling_active = False
                args_list = [args_filter]
            else:
                all_raw_args = list(raw_args_to_value.keys())
                system_present_by_value: dict[str, set[int]] = defaultdict(set)
                q_all = BenchmarkResult.query.filter(
                    BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
                    BenchmarkResult.system_id.in_(sys_id_ints),
                    BenchmarkResult.arguments.in_(all_raw_args),
                ).all()
                for r in q_all:
                    v = raw_args_to_value.get(r.arguments)
                    if v:
                        system_present_by_value[v].add(r.system_id)

                selected_sys_set = set(sys_id_ints)
                common_values = {v for v in value_order if system_present_by_value.get(v, set()) == selected_sys_set}
                non_common_values = [v for v in value_order if v not in common_values]

                axis_raw_args_map: dict[str, list[str]] = {}
                axis_args_list: list[str] = []

                for ra in suite_raw_args_filters:
                    v = raw_args_to_value.get(ra)
                    if not v:
                        continue
                    if v in common_values:
                        if ra not in axis_raw_args_map:
                            axis_args_list.append(ra)
                        axis_raw_args_map[ra] = [ra]

                def _compatible_with_group(v: str, group_values: list[str]) -> bool:
                    v_set = system_present_by_value.get(v, set())
                    for m in group_values:
                        m_set = system_present_by_value.get(m, set())
                        if v_set.intersection(m_set):
                            return False
                    return True

                axis_flag_name = pool_flags[0].lstrip('-') if pool_flags else 'arg'

                seen_groups: set[frozenset[str]] = set()
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
                    if current_base_key and current_base_key != "<pooled>":
                        group_label = f"{current_base_key} --{axis_flag_name} {','.join(sorted_vals)}"
                    else:
                        group_label = f"--{axis_flag_name} {','.join(sorted_vals)}"
                    group_raw_args = [ra for ra in suite_raw_args_filters if raw_args_to_value.get(ra) in gset]
                    axis_raw_args_map[group_label] = group_raw_args
                    axis_args_list.append(group_label)

                if not axis_args_list:
                    pooling_active = False
                    args_list = [args_filter]
                else:
                    args_list = axis_args_list
                    raw_args_for_query_by_args_val = axis_raw_args_map
        elif args_filter is not None:
            args_list = [args_filter]
        else:
            distinct_rows = db.session.query(BenchmarkResult.arguments).filter(
                BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
                BenchmarkResult.system_id.in_(sys_id_ints),
            ).distinct().all()
            args_list = [r[0] for r in distinct_rows]
            if not args_list:
                continue

            if not pool_equivalent_configs:
                from app.option_equivalence import resolution_pool_key, pool_key_for_args
                seen_classes: dict[str, list[str]] = {}
                for a in args_list:
                    if not a or not isinstance(a, str):
                        continue
                    pk = resolution_pool_key(a)
                    if pk:
                        seen_classes.setdefault(pk, []).append(a)
                resolution_raw_map = {}
                pooled_args_list = []
                for a in args_list:
                    if not a or not isinstance(a, str):
                        pooled_args_list.append(a)
                        continue
                    pk = resolution_pool_key(a)
                    if pk and len(seen_classes.get(pk, [])) > 1:
                        sub_key = pool_key_for_args(None, a)
                        if sub_key:
                            if sub_key not in resolution_raw_map:
                                matching = [
                                    ra for ra in seen_classes[pk]
                                    if pool_key_for_args(None, ra) == sub_key
                                ]
                                resolution_raw_map[sub_key] = matching
                                pooled_args_list.append(sub_key)
                        else:
                            pooled_args_list.append(a)
                    else:
                        pooled_args_list.append(a)
                args_list = pooled_args_list

            if pool_equivalent_configs:
                suite_key = (primary_benchmark.title, primary_benchmark.app_version)
                suite_task_key = (suite_key[0], suite_key[1], "pool-axes")
                if suite_task_key in pool_processed_keys:
                    continue
                pool_processed_keys.add(suite_task_key)

                suite_raw_args_filters = [
                    str(a) for a in args_list
                    if isinstance(a, str) and a.strip()
                ]

                raw_args_to_value = {}
                value_order = []
                for ra in suite_raw_args_filters:
                    vals = extract_flag_values(ra, pool_flags)
                    if not vals:
                        continue
                    v0 = str(vals[0]).strip()
                    if not v0:
                        continue
                    raw_args_to_value[ra] = v0
                    if v0 not in value_order:
                        value_order.append(v0)

                if raw_args_to_value:
                    pooling_active = True
                    axis_raw_args_map = {}
                    axis_args_list = []

                    base_to_raws: dict[str, list[str]] = defaultdict(list)
                    for ra in suite_raw_args_filters:
                        base = pool_key_for_args_by_flags(ra, pool_flags) or ra
                        base_to_raws[base].append(ra)

                    axis_flag_name = pool_flags[0].lstrip('-') if pool_flags else 'arg'
                    selected_sys_set = set(sys_id_ints)

                    for base_key, base_raws in base_to_raws.items():
                        base_raws = list(dict.fromkeys(base_raws))
                        base_raw_to_value = {ra: raw_args_to_value.get(ra) for ra in base_raws if raw_args_to_value.get(ra)}
                        if not base_raw_to_value:
                            continue
                        base_values = list(dict.fromkeys(base_raw_to_value.values()))

                        q_all = BenchmarkResult.query.filter(
                            BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
                            BenchmarkResult.system_id.in_(sys_id_ints),
                            BenchmarkResult.arguments.in_(list(base_raw_to_value.keys())),
                        ).all()
                        system_present_by_value = defaultdict(set)
                        for r in q_all:
                            v = base_raw_to_value.get(r.arguments)
                            if v:
                                system_present_by_value[v].add(r.system_id)

                        common_values = {v for v in base_values if system_present_by_value.get(v, set()) == selected_sys_set}
                        non_common_values = [v for v in base_values if v not in common_values]

                        for ra in base_raws:
                            v = base_raw_to_value.get(ra)
                            if not v:
                                continue
                            if v in common_values:
                                if ra not in axis_raw_args_map:
                                    axis_args_list.append(ra)
                                axis_raw_args_map[ra] = [ra]

                        def _compatible_with_group(v: str, group_values: list[str]) -> bool:
                            v_set = system_present_by_value.get(v, set())
                            for m in group_values:
                                m_set = system_present_by_value.get(m, set())
                                if v_set.intersection(m_set):
                                    return False
                            return True

                        seen_groups = set()
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
                            if base_key and base_key != "<pooled>":
                                group_label = f"{base_key} --{axis_flag_name} {','.join(sorted_vals)}"
                            else:
                                group_label = f"--{axis_flag_name} {','.join(sorted_vals)}"
                            group_raw_args = [ra for ra in base_raws if base_raw_to_value.get(ra) in gset]
                            axis_raw_args_map[group_label] = group_raw_args
                            axis_args_list.append(group_label)

                    if axis_args_list:
                        args_list = axis_args_list
                        raw_args_for_query_by_args_val = axis_raw_args_map

        nonempty_primary_args = []
        if not pooling_active:
            nonempty_primary_args = [
                a.strip() for a in args_list
                if isinstance(a, str) and a.strip()
            ]
        _dbg_append(
            step="args_list",
            benchmark_title=primary_benchmark.title,
            benchmark_version=primary_benchmark.app_version,
            args_list=list(args_list),
            resolution_raw_map=(dict(resolution_raw_map) if resolution_raw_map else None),
            nonempty_primary_args=nonempty_primary_args,
        )
        _t("before_args_loop")

        _sensor_parsed_cache: dict[tuple, dict] = {}
        _workload_cache: dict[tuple, dict] = {}
        _all_sensors = Benchmark.query.filter(
            Benchmark.title == primary_benchmark.title,
            Benchmark.app_version == primary_benchmark.app_version,
            Benchmark.display_format == 'LINE_GRAPH',
        ).all()
        _sensor_keywords = (
            'temperature', 'frequency', 'usage', 'power', 'celsius', 'mhz', 'watts',
            'fan', 'rpm', 'voltage', 'energy', 'utilization',
        )
        _all_sensors = [s for s in _all_sensors if s.description and any(k in s.description.lower() for k in _sensor_keywords)]
        _all_sensor_ids = [s.id for s in _all_sensors]
        _all_sensor_results = BenchmarkResult.query.filter(
            BenchmarkResult.benchmark_id.in_(_all_sensor_ids),
            BenchmarkResult.system_id.in_(sys_id_ints),
        ).all()
        _all_by_sensor_sys: dict[tuple[int, int], list[BenchmarkResult]] = defaultdict(list)
        for r in _all_sensor_results:
            _all_by_sensor_sys[(r.benchmark_id, r.system_id)].append(r)
        _args_iter = 0
        for args_val in args_list:
            _t(f"iter_{_args_iter}_start")
            charts = []
            sys_args_map = {}
            system_details = []
            primary_args_set = set()
            resolution_class_name: str | None = None
            resolution_raw_args: list[str] | None = None

            q_prim = BenchmarkResult.query.filter(
                BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
                BenchmarkResult.system_id.in_(sys_id_ints),
            )
            if pooling_active:
                axis_raw = []
                if raw_args_for_query_by_args_val:
                    axis_raw = raw_args_for_query_by_args_val.get(args_val, []) or []
                if not axis_raw:
                    axis_raw = [args_filter]
                q_prim = q_prim.filter(BenchmarkResult.arguments.in_(axis_raw))
            elif args_val is None or (isinstance(args_val, str) and args_val.strip() == ""):
                q_prim = q_prim.filter(
                    (BenchmarkResult.arguments.is_(None)) | (BenchmarkResult.arguments == "")
                )
            else:
                if not pooling_active:
                    from app.option_equivalence import resolution_pool_key
                    # Check if args_val is itself a resolution class key from "All configurations"
                    raw_map_entry = (resolution_raw_map or {}).get(args_val)
                    if raw_map_entry:
                        resolution_raw_args = raw_map_entry
                        resolution_class_name = args_val
                    else:
                        pk = resolution_pool_key(args_val)
                        if pk:
                            matching_raw = db.session.query(BenchmarkResult.arguments).filter(
                                BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
                                BenchmarkResult.system_id.in_(sys_id_ints),
                                BenchmarkResult.arguments.isnot(None),
                                BenchmarkResult.arguments != "",
                            ).distinct().all()
                            all_raw = list({r[0] for r in matching_raw if r[0]})
                            same_class = [
                                ra for ra in all_raw
                                if resolution_pool_key(ra) == pk
                            ]
                            if len(same_class) > 1:
                                resolution_raw_args = same_class
                                resolution_class_name = pk
                if resolution_raw_args:
                    q_prim = q_prim.filter(BenchmarkResult.arguments.in_(resolution_raw_args))
                else:
                    q_prim = q_prim.filter(BenchmarkResult.arguments == args_val)
            all_prim_results = q_prim.all()
            _t(f"iter_{_args_iter}_prim_results_queried")

            _dbg_append(
                step="raw_results",
                benchmark_identifier=primary_benchmark.identifier,
                benchmark_title=primary_benchmark.title,
                benchmark_version=primary_benchmark.app_version,
                args_val=args_val,
                primary_bm_ids=matching_primary_bm_ids,
                results=[{
                    "id": r.id,
                    "system_id": r.system_id,
                    "benchmark_id": r.benchmark_id,
                    "arguments": r.arguments,
                    "value": r.value,
                    "import_batch_id": r.import_batch_id,
                    "imported_at": str(r.imported_at) if r.imported_at else None,
                } for r in all_prim_results],
            )

            by_bm_id = defaultdict(list)
            for r in all_prim_results:
                by_bm_id[r.benchmark_id].append(r)
                sys_args_map[r.system_id] = r.arguments
                if r.arguments:
                    primary_args_set.add(r.arguments.strip())

            _dbg_append(
                step="iter_query",
                args_val=args_val,
                resolution_class_name=resolution_class_name,
                resolution_raw_args=resolution_raw_args if resolution_raw_args else None,
                systems_with_results=sorted(sys_args_map.keys()) if sys_args_map else [],
                systems_without_results=sorted(set(sys_id_ints) - set(sys_args_map.keys())),
                result_count=len(all_prim_results),
            )

            if resolution_class_name:
                primary_args_set = {resolution_class_name}

            for sys_id in sys_id_ints:
                if sys_id not in sys_args_map:
                    continue
                system = systems_by_id.get(sys_id)
                if not system:
                    continue
                if not any(s['id'] == sys_id for s in system_details):
                    system_details.append({
                        'id': sys_id,
                        'short_name': system.identifier,
                        'full_label': format_system_profile_label(system)
                    })

            primary_benchmarks = Benchmark.query.filter(
                Benchmark.id.in_(by_bm_id.keys())
            ).all()

            if pooling_active:
                bm_by_id = {bm.id: bm for bm in primary_benchmarks}
                sig_to_rows: dict[tuple[str, str, str, str], list[BenchmarkResult]] = defaultdict(list)
                for r in all_prim_results:
                    bm = bm_by_id.get(r.benchmark_id)
                    if not bm:
                        continue
                    sig = (
                        (bm.description or "").strip(),
                        (bm.scale or "").strip(),
                        (bm.proportion or "").strip(),
                        (bm.display_format or "").strip(),
                    )
                    sig_to_rows[sig].append(r)

                sys_args_map = {}
                first_sig = True

                for sig, sig_rows in sig_to_rows.items():
                    desc_sig, scale_sig, prop_sig, disp_sig = sig
                    prop = (prop_sig or "").strip().upper()
                    lower_better = prop == "LIB"
                    primary_traces = []
                    sys_ids_with_results = set()
                    for sys_id in sys_id_ints:
                        candidates = [r for r in sig_rows if r.system_id == sys_id]
                        if not candidates:
                            continue
                        sys_ids_with_results.add(sys_id)
                        def score_key(r):
                            v = r.value
                            if v is None:
                                return float("inf") if lower_better else float("-inf")
                            return float(v)
                        res = min(candidates, key=score_key) if lower_better else max(candidates, key=score_key)
                        if first_sig:
                            sys_args_map[sys_id] = res.arguments
                        system = systems_by_id.get(sys_id)
                        if not system:
                            continue
                        system_label = format_system_profile_label(system)
                        short_name = system.identifier
                        suffix = _pooled_flag_suffix_from_args(res.arguments)
                        trace_name = f"{short_name} ({suffix})" if suffix else short_name
                        trace = {
                            "name": trace_name,
                            "type": "bar",
                            "customdata": [system_label + (f" ({suffix})" if suffix else "")],
                            "hovertemplate": "%{customdata[0]}<br>%{x}<extra></extra>",
                            "x": [trace_name],
                            "y": [res.value],
                        }
                        primary_traces.append(trace)
                    if primary_traces:
                        metric_label = (desc_sig or "").strip() or (scale_sig or "Primary Result")
                        charts.append({
                            "metric": metric_label,
                            "description": desc_sig,
                            "scale": scale_sig,
                            "display_format": disp_sig or "BAR_GRAPH",
                            "proportion": prop_sig,
                            "options": sorted(primary_args_set),
                            "traces": primary_traces,
                            "is_primary": True
                        })
                    first_sig = False
            else:
                if resolution_class_name:
                    first_bm = next(iter(sorted(primary_benchmarks, key=lambda x: x.id)), None)
                    if first_bm:
                        merged_traces = []
                        for sys_id in sys_id_ints:
                            system = systems_by_id.get(sys_id)
                            if not system:
                                continue
                            sys_results = [r for r in all_prim_results if r.system_id == sys_id]
                            if not sys_results:
                                continue
                            lower_better = (first_bm.proportion or "").strip().upper() == "LIB"
                            best = (
                                min(sys_results, key=lambda r: float("inf") if r.value is None else float(r.value))
                                if lower_better
                                else max(sys_results, key=lambda r: float("-inf") if r.value is None else float(r.value))
                            )
                            obs_label = format_observation_label(system, best.profile_snapshot, best.imported_at)
                            system_label = format_system_profile_label(system)
                            args_label = best.arguments or ""
                            trace = {
                                "name": f"{obs_label} ({args_label})" if args_label else obs_label,
                                "type": "bar" if first_bm.display_format == "BAR_GRAPH" else "scatter",
                                "customdata": [[system_label, obs_label]],
                                "import_batch_id": best.import_batch_id,
                            }
                            if first_bm.display_format == "BAR_GRAPH":
                                trace["x"] = [obs_label]
                                trace["y"] = [best.value]
                            elif first_bm.display_format == "LINE_GRAPH":
                                from app.sensor_quality import numeric_series
                                y_data = numeric_series(best.data_json)
                                trace["x"] = list(range(len(y_data)))
                                trace["y"] = y_data
                                trace["mode"] = "lines"
                            merged_traces.append(trace)
                        if merged_traces:
                            charts.append({
                                "metric": resolution_class_name,
                                "description": first_bm.description,
                                "scale": first_bm.scale,
                                "display_format": first_bm.display_format,
                                "proportion": first_bm.proportion,
                                "options": sorted(primary_args_set),
                                "traces": merged_traces,
                                "is_primary": True,
                            })
                else:
                    for bm in sorted(primary_benchmarks, key=lambda x: x.id):
                        results_for_bm = by_bm_id.get(bm.id, [])
                        if not results_for_bm:
                            continue
                        primary_traces = []
                        sys_ids_with_results = set()
                        for sys_id in sys_id_ints:
                            system = systems_by_id.get(sys_id)
                            if not system:
                                continue
                            matching = [r for r in results_for_bm if r.system_id == sys_id]
                            if not matching:
                                continue
                            sys_ids_with_results.add(sys_id)
                            for res in sorted(matching, key=lambda r: (r.imported_at or "", r.id)):
                                obs_label = format_observation_label(
                                    system, res.profile_snapshot, res.imported_at,
                                )
                                system_label = format_system_profile_label(system)
                                trace = {
                                    "name": obs_label,
                                    "type": "bar" if bm.display_format == "BAR_GRAPH" else "scatter",
                                    "customdata": [[system_label, obs_label]],
                                    "hovertemplate": (
                                        "%{customdata[0][0]}<br>%{customdata[0][1]}<br>%{x}<extra></extra>"
                                        if bm.display_format == "BAR_GRAPH"
                                        else "%{customdata[0][0]}<br>%{customdata[0][1]}<extra></extra>"
                                    ),
                                    "import_batch_id": res.import_batch_id,
                                }
                                if bm.display_format == "BAR_GRAPH":
                                    trace["x"] = [obs_label]
                                    trace["y"] = [res.value]
                                elif bm.display_format == "LINE_GRAPH":
                                    from app.sensor_quality import numeric_series
                                    y_data = numeric_series(res.data_json)
                                    trace["x"] = list(range(len(y_data)))
                                    trace["y"] = y_data
                                    trace["mode"] = "lines"
                                primary_traces.append(trace)
                        if primary_traces:
                            metric_label = (bm.description or "").strip() or (bm.scale or "Primary Result")
                            charts.append({
                                "metric": metric_label,
                                "description": bm.description,
                                "scale": bm.scale,
                                "display_format": bm.display_format,
                                "proportion": bm.proportion,
                                "options": sorted(primary_args_set),
                                "traces": primary_traces,
                                "is_primary": True
                            })
            _t(f"iter_{_args_iter}_charts_built")

            _dbg_append(
                step="charts_data",
                args_val=args_val,
                resolution_class=resolution_class_name,
                charts=[{
                    "metric": ch.get("metric"),
                    "description": ch.get("description"),
                    "scale": ch.get("scale"),
                    "proportion": ch.get("proportion"),
                    "display_format": ch.get("display_format"),
                    "is_primary": ch.get("is_primary"),
                    "options": ch.get("options"),
                    "trace_values": [
                        {"name": t.get("name"), "y": t.get("y"), "customdata": t.get("customdata")}
                        for t in ch.get("traces", [])
                    ],
                    "_inferred_HIB": not proportion_is_lower_better(ch.get("proportion")),
                } for ch in charts],
            )

            from app.workload_profile import (
                build_workload_profile,
                option_profile_key,
                sensor_is_relevant,
            )
            from app.sensor_quality import chart_has_usable_signal, series_quality

            config_args_for_wl = args_val if args_val is not None else ""
            workload_profiles_by_option: dict[str, dict] = {}
            _t(f"iter_{_args_iter}_workload_start")
            for ch in charts:
                if not ch.get("is_primary"):
                    continue
                desc = (ch.get("description") or "").strip()
                scale = (ch.get("scale") or "").strip()
                ok = option_profile_key(desc, scale)
                if ok in workload_profiles_by_option:
                    ch["option_key"] = ok
                    ch["workload_profile"] = workload_profiles_by_option[ok]
                    continue
                _wl_cache_key = (config_args_for_wl, ok)
                _cached_wl = _workload_cache.get(_wl_cache_key)
                if _cached_wl is not None:
                    wl = _cached_wl
                else:
                    wl = build_workload_profile(
                        primary_benchmark.title,
                        primary_benchmark.app_version or "",
                        config_args_for_wl,
                        system_ids=sys_id_ints,
                        description=desc or primary_benchmark.description or "",
                        option_description=desc,
                        option_scale=scale,
                    )
                    _workload_cache[_wl_cache_key] = wl
                workload_profiles_by_option[ok] = wl
                ch["option_key"] = ok
                ch["workload_profile"] = wl

            workload_profile = (
                next(iter(workload_profiles_by_option.values()))
                if len(workload_profiles_by_option) == 1
                else None
            )
            _t(f"iter_{_args_iter}_workload_done")
            filter_sensors = (request.args.get('filter_sensors') or '1').lower() not in {'0', 'false', 'no'}
            filter_noisy = (request.args.get('filter_noisy_sensors') or '1').lower() not in {'0', 'false', 'no'}
            if filter_sensors and workload_profiles_by_option:
                sensors = [
                    s for s in _all_sensors
                    if any(
                        sensor_is_relevant(s.description, s.scale, wp, strict=True)
                        for wp in workload_profiles_by_option.values()
                    )
                ]
            else:
                sensors = list(_all_sensors)

            sensor_ids = [s.id for s in sensors]
            _t(f"iter_{_args_iter}_sensor_query_start")
            by_sensor_sys: dict[tuple[int, int], list[BenchmarkResult]] = {}
            for key, results in _all_by_sensor_sys.items():
                bm_id, sys_id = key
                if bm_id in sensor_ids and sys_id in sys_args_map:
                    by_sensor_sys[key] = results
            _t(f"iter_{_args_iter}_sensor_query_done")

            for s_bm in sensors:
                s_traces = []
                for sys_id in sys_args_map:
                    target_args = sys_args_map[sys_id]
                    system = systems_by_id.get(sys_id)

                    all_s_res = by_sensor_sys.get((s_bm.id, sys_id), [])
                    if not target_args:
                        if nonempty_primary_args:
                            matching_s_res = [
                                r for r in all_s_res
                                if not any(pa in (r.arguments or "") for pa in nonempty_primary_args)
                            ]
                        else:
                            matching_s_res = list(all_s_res)
                    else:
                        exact = [
                            r for r in all_s_res
                            if (r.arguments or "").strip() == target_args.strip()
                        ]
                        matching_s_res = exact if exact else [
                            r for r in all_s_res
                            if target_args in (r.arguments or "")
                        ]

                    if not matching_s_res:
                        continue
                    batches = sorted({
                        (r.import_batch_id or f"legacy-{r.id}") for r in matching_s_res
                    })
                    for batch_key in batches:
                        batch_rows = [
                            r for r in matching_s_res
                            if (r.import_batch_id or f"legacy-{r.id}") == batch_key
                        ]
                        if not batch_rows:
                            continue
                        s_res = batch_rows[0]
                        obs_label = format_observation_label(
                            system, s_res.profile_snapshot, s_res.imported_at,
                        )
                        system_label = format_system_profile_label(system)
                        short_name = obs_label

                        trace = {
                            "name": short_name,
                            "type": "bar" if s_bm.display_format == "BAR_GRAPH" else "scatter",
                            "customdata": [[system_label, obs_label]],
                            "hovertemplate": (
                                "%{customdata[0][0]}<br>%{customdata[0][1]}<extra></extra>"
                            ),
                            "import_batch_id": s_res.import_batch_id,
                        }
                        if s_bm.display_format == "BAR_GRAPH":
                            trace["x"] = [short_name]
                            trace["y"] = [s_res.value]
                        elif s_bm.display_format == "LINE_GRAPH":
                            _ckey = (s_bm.id, sys_id, s_res.import_batch_id)
                            _cached = _sensor_parsed_cache.get(_ckey)
                            if _cached is not None:
                                y_data, stats_dict, quality = _cached
                            else:
                                from app.sensor_quality import numeric_series
                                y_data = numeric_series(s_res.data_json)
                                stats_dict = {}
                                quality = None
                                if y_data:
                                    clean_y = [val for val in y_data if isinstance(val, (int, float))]
                                    if clean_y:
                                        import statistics
                                        stats_dict = {
                                            "min": min(clean_y),
                                            "max": max(clean_y),
                                            "mean": statistics.mean(clean_y),
                                            "median": statistics.median(clean_y)
                                        }
                                        try:
                                            qs = statistics.quantiles(clean_y, n=4, method="inclusive")
                                            if len(qs) >= 3:
                                                stats_dict["q1"] = qs[0]
                                                stats_dict["q3"] = qs[2]
                                        except (statistics.StatisticsError, ValueError):
                                            pass
                                        quality = series_quality(clean_y, s_bm.description, s_bm.scale)
                                _sensor_parsed_cache[_ckey] = (y_data, stats_dict, quality)
                            trace["x"] = list(range(len(y_data)))
                            trace["y"] = y_data
                            trace["mode"] = "lines"
                            if quality is not None:
                                stats_dict["quality"] = quality
                                trace["quality"] = quality
                            if stats_dict:
                                trace["stats"] = stats_dict

                        s_traces.append(trace)

                has_signal, noise_reason = chart_has_usable_signal(
                    s_traces, s_bm.description or "", s_bm.scale or "",
                )
                if filter_noisy and not has_signal:
                    continue

                if s_traces:
                    metric_label = s_bm.description
                    if 'CPU Frequency' in s_bm.description:
                        metric_label = "CPU Freq"
                    elif 'CPU Temperature' in s_bm.description:
                        metric_label = "CPU Temp"
                    elif 'CPU Usage' in s_bm.description:
                        metric_label = "CPU Usage"
                    elif 'CPU Power' in s_bm.description:
                        metric_label = "CPU Power"

                    option_relevance = {
                        ok: sensor_is_relevant(s_bm.description, s_bm.scale, wp, strict=True)
                        for ok, wp in workload_profiles_by_option.items()
                    }
                    charts.append({
                        "metric": metric_label,
                        "description": s_bm.description,
                        "scale": s_bm.scale,
                        "display_format": s_bm.display_format,
                        "proportion": s_bm.proportion,
                        "traces": s_traces,
                        "is_primary": False,
                        "sensor_quality": {
                            "has_signal": has_signal,
                            "noise_reason": noise_reason,
                        },
                        "option_workload_relevant": option_relevance,
                    })

            _t(f"iter_{_args_iter}_sensors_done")
            if charts:
                charts.sort(key=lambda x: not x["is_primary"])
                title = f"{primary_benchmark.title} ({primary_benchmark.app_version})"
                display_args = resolution_class_name or (args_val if isinstance(args_val, str) else "")
                if display_args:
                    title += f" — {display_args}"
                args_label = None
                if pooling_active:
                    args_label = args_val
                elif resolution_class_name:
                    args_label = resolution_class_name
                elif args_val and (isinstance(args_val, str) and args_val.strip()):
                    args_label = args_val
                else:
                    first_sys_id = sys_id_ints[0] if sys_id_ints else None
                    if first_sys_id:
                        q_prim = BenchmarkResult.query.filter(
                            BenchmarkResult.system_id == first_sys_id,
                            BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
                        )
                        if args_val is None or (isinstance(args_val, str) and not args_val.strip()):
                            q_prim = q_prim.filter(
                                (BenchmarkResult.arguments.is_(None)) | (BenchmarkResult.arguments == "")
                            )
                        else:
                            q_prim = q_prim.filter(BenchmarkResult.arguments == args_val)
                        prim_res = q_prim.first()
                        if prim_res:
                            bm_for_args = db.session.get(Benchmark, prim_res.benchmark_id)
                            if bm_for_args and bm_for_args.description:
                                other_bms = Benchmark.query.filter(
                                    Benchmark.title == primary_benchmark.title,
                                    Benchmark.app_version == primary_benchmark.app_version,
                                    Benchmark.display_format == "BAR_GRAPH",
                                    Benchmark.id != bm_for_args.id,
                                ).all()
                                other_descriptions = [b.description for b in other_bms if b.description]
                                args_label = _unique_part_of_description(
                                    bm_for_args.description, other_descriptions
                                ) or (args_val if isinstance(args_val, str) else "")
                system_names = []
                for sid in sys_id_ints:
                    sys_obj = systems_by_id.get(sid)
                    if sys_obj:
                        system_names.append(sys_obj.identifier)
                _pts_timings: list[tuple[str, float]] = []
                pts_scoring = build_pts_context_for_compare_group(
                    title=primary_benchmark.title,
                    app_version=primary_benchmark.app_version or "",
                    identifier=primary_benchmark.identifier,
                    primary_charts=[c for c in charts if c.get("is_primary")],
                    system_ids=system_names,
                    config_args=args_val if args_val is not None else "",
                    ob_index=ob_index_cache,
                    _timings_out=_pts_timings,
                )
                # merge pts timings into main timeline for the profile
                for label, ts in _pts_timings:
                    _timings.append((f"iter_{_args_iter}_pts_{label}", ts))
                _t(f"iter_{_args_iter}_pts_context_built")
                sub_by_desc = {
                    (st.get("description") or "").strip(): st
                    for st in (pts_scoring.get("subtests") or [])
                }
                for ch in charts:
                    if not ch.get("is_primary"):
                        continue
                    st = sub_by_desc.get((ch.get("description") or "").strip())
                    if st:
                        ch["pts"] = {
                            "comparison_hash": st.get("comparison_hash"),
                            "pts_relative": st.get("pts_relative"),
                            "pts_ob_relative": st.get("pts_ob_relative"),
                            "pts_ob_p1_relative": st.get("pts_ob_p1_relative"),
                            "ob_percentile": st.get("ob_percentile"),
                            "ob": st.get("ob"),
                        }

                comparison_groups.append({
                    "title": title,
                    "charts": charts,
                    "system_details": system_details,
                    "args": args_val if args_val is not None else "",
                    "args_label": args_label or args_val or "",
                    "workload_profile": workload_profile,
                    "workload_profiles_by_option": workload_profiles_by_option,
                    "pts_scoring": pts_scoring,
                })

                system_id_order = [s["id"] for s in system_details]
                subtest_math: list[dict] = []
                for ch in charts:
                    if not ch.get("is_primary") or ch.get("display_format") != "BAR_GRAPH":
                        continue
                    traces = ch.get("traces", [])
                    sys_values: list[float | None] = []
                    sys_values_map: dict[int, float | None] = {}
                    for i, t in enumerate(traces):
                        y = t.get("y", [])
                        val = float(y[0]) if y and y[0] is not None else None
                        sys_id = system_id_order[i] if i < len(system_id_order) else None
                        if sys_id is not None:
                            sys_values_map[sys_id] = val
                        sys_values.append(val)
                    present = [v for v in sys_values if v is not None and v > 0]
                    prop = ch.get("proportion")
                    HIB = not proportion_is_lower_better(prop)
                    G = None
                    if len(present) >= 2:
                        import math
                        G = math.exp(sum(math.log(v) for v in present) / len(present))
                    pcts = [((v / G - 1) * 100) if HIB else ((G / v - 1) * 100)
                            if v is not None and v > 0 and G and G > 0 else None
                            for v in sys_values]
                    subtest_math.append({
                        "metric": ch.get("metric"),
                        "description": ch.get("description"),
                        "proportion": prop,
                        "scale": ch.get("scale"),
                        "HIB": HIB,
                        "system_values": sys_values_map,
                        "system_values_ordered": sys_values,
                        "geometric_mean": round(G, 6) if G is not None else None,
                        "n_systems_used": len(present),
                        "pcts_ordered": [round(p, 6) if p is not None else None for p in pcts],
                    })
                _dbg_append(
                    step="subtest_math",
                    args_val=args_val,
                    title=title,
                    system_details=system_details,
                    subtests=subtest_math,
                )

                _t(f"iter_{_args_iter}_done")
            _args_iter += 1

    if not comparison_groups:
        return {"error": "Could not find benchmark data"}, 404

    pts_contexts = [g.get("pts_scoring") for g in comparison_groups if g.get("pts_scoring")]
    first_names = []
    if comparison_groups and comparison_groups[0].get("system_details"):
        first_names = [s.get("short_name") for s in comparison_groups[0]["system_details"] if s.get("short_name")]
    pts_global = (
        build_pts_global_summary(comparison_groups, pts_contexts=pts_contexts)
        if comparison_groups else None
    )
    pts_global_harmonic = (
        build_pts_global_harmonic_summary(comparison_groups)
        if comparison_groups else None
    )
    _t("global_harmonic_done")
    pts_global_ob = (
        build_pts_ob_global_summary(pts_contexts, first_names)
        if pts_contexts and first_names else None
    )
    _t("done")

    _dbg_append(
        step="global_math",
        group_count=len(comparison_groups),
        system_ids=sys_id_ints,
        pts_global=pts_global,
        pts_global_harmonic=pts_global_harmonic,
        pts_global_ob=pts_global_ob,
    )

    _timings_dict = {}
    for i in range(1, len(_timings)):
        label = _timings[i][0]
        secs = round(_timings[i][1] - _timings[i-1][1], 3)
        _timings_dict[label] = secs

    _meta = {
        "system_ids": system_ids,
        "config_count": len(config_list),
        "benchmark_ids": benchmark_ids,
        "group_count": len(comparison_groups),
        "timestamp": time.time(),
    }
    _save_profile(_meta)

    resp = {
        "comparison_groups": comparison_groups,
        "scoring_engine": "pts" if pts_contexts else "benchviz",
        "pts": {
            "ob_index_available": ob_index_cache is not None,
            "ob_index_synced_at": (ob_index_cache or {}).get("synced_at"),
            "ob_entry_count": (ob_index_cache or {}).get("entry_count"),
            "global": pts_global,
            "global_harmonic_by_scale": (pts_global_harmonic or {}).get("by_scale") if pts_global_harmonic else None,
            "global_harmonic_cross_scale": (pts_global_harmonic or {}).get("cross_scale") if pts_global_harmonic else None,
            "global_ob": pts_global_ob,
        },
        "_timings": _timings_dict,
    }
    try:
        with open(_COMPARE_DEBUG_PATH, "w") as f:
            json.dump(_dbg, f, default=str)
    except Exception:
        pass
    return resp


@bp.route('/api/pool_flag_suggestions')
def api_pool_flag_suggestions():
    from app.args_pooling import parse_args_tokens

    system_ids = request.args.getlist('system_ids')
    config_params = request.args.getlist('config')
    if not system_ids:
        return {"error": "Missing system_ids parameter(s)"}, 400

    try:
        sys_id_ints = [int(s) for s in system_ids]
    except (TypeError, ValueError):
        return {"error": "Invalid system_ids"}, 400

    config_list: list[tuple[int, str | None]] = []
    for c in config_params:
        part = (c or "").strip()
        if not part:
            continue
        if "|" in part:
            b_id_str, args_str = part.split("|", 1)
            try:
                b_id = int((b_id_str or "").strip())
            except (TypeError, ValueError):
                continue
            args_val = (args_str or "").strip() or None
            if args_val:
                try:
                    args_val = unquote(args_val)
                except Exception:
                    pass
            config_list.append((b_id, args_val))
        else:
            try:
                b_id = int(part)
            except (TypeError, ValueError):
                continue
            config_list.append((b_id, None))

    if not config_list:
        return {"error": "Missing config parameter(s)"}, 400

    suite_to_selected_args: dict[tuple[str, str], set[str | None]] = defaultdict(set)
    for b_id, args_val in config_list:
        bm = db.session.get(Benchmark, b_id)
        if not bm:
            continue
        if not getattr(bm, "is_primary", False):
            cand = BenchmarkRepository.find_first_primary(bm.title, bm.app_version)
            if cand:
                bm = cand
        suite_key = (bm.title, bm.app_version or "")
        suite_to_selected_args[suite_key].add(args_val)

    if not suite_to_selected_args:
        return {"candidates": [], "samples": []}, 200

    selected_sys_set = set(sys_id_ints)
    flag_value_systems: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    sample_rows: list[dict] = []

    def parse_flag_pairs(args_text: str) -> list[tuple[str, str]]:
        toks = parse_args_tokens(args_text)
        out: list[tuple[str, str]] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if not isinstance(t, str):
                i += 1
                continue
            if t.startswith("--"):
                if "=" in t:
                    f, v = t.split("=", 1)
                    if f and v:
                        out.append((f, v))
                else:
                    if i + 1 < len(toks):
                        nxt = toks[i + 1]
                        if isinstance(nxt, str) and not nxt.startswith("-"):
                            out.append((t, nxt))
                            i += 1
            elif t.startswith("-") and len(t) >= 2:
                if len(t) == 2:
                    if i + 1 < len(toks):
                        nxt = toks[i + 1]
                        if isinstance(nxt, str) and not nxt.startswith("-"):
                            out.append((t, nxt))
                            i += 1
                else:
                    out.append((t[:2], t[2:]))
            i += 1
        return out

    for (title, app_ver), selected_args in suite_to_selected_args.items():
        ids_with_results = [
            r[0] for r in db.session.query(BenchmarkResult.benchmark_id)
            .filter(BenchmarkResult.system_id.in_(sys_id_ints))
            .distinct().all()
        ]
        bm_ids = [
            bm.id for bm in Benchmark.query.filter(
                Benchmark.id.in_(ids_with_results),
                Benchmark.title == title,
                Benchmark.app_version == app_ver,
                Benchmark.display_format == "BAR_GRAPH",
                Benchmark.is_primary == True,
            ).all()
        ]
        if not bm_ids:
            continue

        q = BenchmarkResult.query.filter(
            BenchmarkResult.benchmark_id.in_(bm_ids),
            BenchmarkResult.system_id.in_(sys_id_ints),
        )

        explicit_args = {a for a in selected_args if isinstance(a, str) and a.strip()}
        has_all_configs = any(a is None for a in selected_args)
        if explicit_args and not has_all_configs:
            q = q.filter(BenchmarkResult.arguments.in_(list(explicit_args)))

        rows = q.all()
        for r in rows:
            a = (r.arguments or "").strip()
            if not a:
                continue
            pairs = parse_flag_pairs(a)
            for f, v in pairs:
                fv = str(v).strip()
                if not fv:
                    continue
                flag_value_systems[f][fv].add(r.system_id)
            if len(sample_rows) < 200:
                sample_rows.append({
                    "benchmark_title": title,
                    "app_version": app_ver,
                    "system_id": r.system_id,
                    "args": a,
                })

    candidates = []
    for flag, value_map in flag_value_systems.items():
        values = sorted(value_map.keys())
        if len(values) < 2:
            continue
        shared_values = [v for v in values if value_map[v] == selected_sys_set]
        non_shared_values = [v for v in values if value_map[v] != selected_sys_set]
        if not non_shared_values:
            continue
        coverage = len(set().union(*value_map.values())) if value_map else 0
        score = len(non_shared_values) * 100 + coverage
        candidates.append({
            "flag": flag,
            "score": score,
            "distinct_values": values,
            "shared_values": shared_values,
            "non_shared_values": non_shared_values,
        })

    candidates.sort(key=lambda x: (x["score"], len(x["distinct_values"])), reverse=True)

    top_candidates = candidates[:3]
    wanted_pairs: list[tuple[str, str]] = []
    for c in top_candidates:
        f = c["flag"]
        for v in c["non_shared_values"]:
            wanted_pairs.append((f, str(v)))

    sample_out: list[dict] = []
    picked_pairs: set[tuple[str, str]] = set()
    for flag, value in wanted_pairs:
        best_row = None
        best_len = None
        for row in sample_rows:
            pairs = parse_flag_pairs(row["args"])
            hit = any((pf == flag and str(pv).strip() == value) for pf, pv in pairs)
            if not hit:
                continue
            ln = len(row.get("args") or "")
            if best_row is None or ln < (best_len or 10**9):
                best_row = row
                best_len = ln
        if best_row is not None:
            key = (flag, value)
            if key not in picked_pairs:
                sample_out.append(best_row)
                picked_pairs.add(key)

    sample_out = sample_out[:18]

    return {"candidates": candidates[:20], "samples": sample_out}, 200


@bp.route('/api/profile/last-comparison')
def api_profile_last_comparison():
    """Return profiling data from the last /api/compare call."""
    if not os.path.exists(_PROFILE_PATH):
        return {"error": "No profile data available"}, 404
    try:
        with open(_PROFILE_PATH) as f:
            data = json.load(f)
        return data, 200
    except Exception as e:
        return {"error": str(e)}, 500


_ML_PROFILE_PATH = "/tmp/benchviz_last_ml_profile.json"

@bp.route('/api/profile/last-ml')
def api_profile_last_ml():
    """Return profiling data from the last ML rebuild (rebuild-all-insights)."""
    if not os.path.exists(_ML_PROFILE_PATH):
        return {"error": "No ML profile data available"}, 404
    try:
        with open(_ML_PROFILE_PATH) as f:
            data = json.load(f)
        return data, 200
    except Exception as e:
        return {"error": str(e)}, 500


_COMPARE_DEBUG_PATH = "/tmp/benchviz_last_compare_debug.json"

@bp.route('/api/compare-debug')
def api_compare_debug():
    """
    Debug endpoint: runs the full comparison pipeline with detailed math tracing.
    Accepts the same query parameters as /api/compare.
    Returns the full debug trace from the comparison (saved to
    /tmp/benchviz_last_compare_debug.json).
    """
    resp = api_compare()
    if isinstance(resp, tuple):
        payload, status = resp
    else:
        payload, status = resp, 200
    if status != 200:
        return payload, status
    if not os.path.exists(_COMPARE_DEBUG_PATH):
        return {"error": "No debug data generated"}, 500
    try:
        with open(_COMPARE_DEBUG_PATH) as f:
            data = json.load(f)
        return data, 200
    except Exception as e:
        return {"error": str(e)}, 500


@bp.route('/api/profile/last-compare-debug')
def api_profile_last_compare_debug():
    """Return the last saved compare debug trace from /tmp."""
    if not os.path.exists(_COMPARE_DEBUG_PATH):
        return {"error": "No compare debug data available"}, 404
    try:
        with open(_COMPARE_DEBUG_PATH) as f:
            data = json.load(f)
        return data, 200
    except Exception as e:
        return {"error": str(e)}, 500
