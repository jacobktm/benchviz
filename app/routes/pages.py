from __future__ import annotations

import datetime
import json
import math
import os
import re
import secrets
import shutil
import statistics
import tempfile
import threading
import uuid
import zipfile
from collections import defaultdict

from flask import (
    Blueprint, current_app, flash, jsonify, redirect,
    render_template, request, send_file, url_for,
)
from werkzeug.utils import secure_filename
from urllib.parse import unquote
import sqlalchemy as sa
from sqlalchemy import or_, func as sqla_func

from app import db
from app.models import (
    System, Benchmark, BenchmarkResult, SystemNvmeConfig,
    BenchmarkAnalysis, SavedComparison, HardwareSpec, SpecFieldSchema,
)
from app.hardware_spec import (
    auto_populate_hardware_spec, apply_sidecar_to_spec,
    is_sidecar_filename, system_id_from_sidecar_filename,
    missing_spec_hints,
)
from app.parser import (
    BOOL_PROFILE_FIELDS, STRING_PROFILE_FIELDS,
    parse_benchmark_files, parse_file, pop_import_notes,
)
from app.result_merge import bar_run_values
from app.profile_snapshot import format_observation_label
from app.benchmark_util import delete_orphan_benchmarks, delete_system_benchmark_suite
from app.analyzer import analyze_benchmarks
from app.ml.analyzer import analyze_ml_profiles
from app.ml.hardware_ranking import list_rankable_benchmarks
from app.analyzer import INSIGHT_COMPONENT_KEYS
from app.insights_lock import insights_rebuild_lock
from app.insights_runner import schedule_insights_rebuild
from app.pts import proportion_is_lower_better
from app.repositories import BenchmarkRepository, SystemRepository
from app.components import (
    clean_text, extract_hardware_component,
    get_primary_group_name, get_system_components,
    normalize_graphics_name, normalize_processor_name,
)
from app.system_util import base_system_identifier, hardware_fingerprint
from app.route_helpers import (
    geometric_mean_positive,
    geometric_mean_by_system_across_arguments,
    get_unique_field_values,
    checkbox_value,
    build_system_profile_from_form,
    split_component_list,
    extract_storage_drives,
    sync_nvme_configs,
    get_profile_badges,
    format_system_profile_label,
    get_system_search_tags,
    group_system_profiles,
    serialize_compare_system_groups,
    _reconcile_primary_name_conflict,
    COMPARE_BY_OPTIONS,
    _insights_infer_scope,
    _insights_workload_context_from_analysis,
    _insights_allowed_singles_for_scope,
    _load_primary_insights_bundle,
    _insights_signal_to_noise_raw,
    _insights_alignment_tier,
    _insights_alignment_rank_score,
    _insights_eta_squared_norm_buckets,
    generate_comparison_id,
    _unique_part_of_description,
)

bp = Blueprint("pages", __name__)


def _import_sidecar_json(filepath: str) -> None:
    """Load a hardware-spec-*.json file and update the matching system's HardwareSpec."""
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        flash(f'Could not parse sidecar JSON: {e}', 'error')
        return

    if not isinstance(data, dict):
        flash('Sidecar JSON must be an object.', 'error')
        return

    system_id_str = system_id_from_sidecar_filename(os.path.basename(filepath))
    system = System.query.filter_by(identifier=system_id_str).first()
    if not system:
        flash(f'No system found for sidecar identifier "{system_id_str}"', 'warning')
        return

    spec = HardwareSpec.query.filter_by(system_id=system.id).first()
    if spec is None:
        spec = HardwareSpec(system_id=system.id, source='sidecar')
        db.session.add(spec)

    spec.source = 'sidecar'
    apply_sidecar_to_spec(spec, data)
    db.session.flush()
    flash(f'Hardware spec updated from sidecar for "{system_id_str}".', 'success')


@bp.route('/')
def dashboard():
    removed_orphans = delete_orphan_benchmarks()
    if removed_orphans:
        db.session.commit()

    systems_raw = System.query.all()
    grouped_systems = group_system_profiles(systems_raw)

    # Perf counters are stored as BAR_GRAPH benchmarks too, but marked non-primary.
    # We exclude them from the dashboard "benchmarks" listing for clarity.
    primary_benchmarks = BenchmarkRepository.find_primary_with_results("")
    
    dashboard_groups = {}
    for p_bm in primary_benchmarks:
        id_str = f" [{p_bm.identifier}]" if p_bm.identifier else ""
        key = f"{p_bm.title} ({p_bm.app_version}){id_str}"
        if key not in dashboard_groups:
            # Find sensors associated with this title
            sensors = Benchmark.query.filter_by(
                identifier=p_bm.identifier,
                title=p_bm.title, 
                app_version=p_bm.app_version, 
                display_format='LINE_GRAPH'
            ).all()
            
            dashboard_groups[key] = {
                'title': p_bm.title,
                'app_version': p_bm.app_version,
                'identifier': p_bm.identifier,
                'runs': [],
                'sensors': sensors,
                'search_tags': set(),
                'system_variations_map': {}
            }
            
        dashboard_groups[key]['runs'].append(p_bm)
        
        # Add the hardware, software, and identifiers of the systems that ran this benchmark
        for res in p_bm.results:
            sys = res.system
            sys.primary_group_name = get_primary_group_name(sys)
            sys.profile_label = format_system_profile_label(sys)
            dashboard_groups[key]['system_variations_map'][sys.id] = sys
            dashboard_groups[key]['search_tags'].update(get_system_search_tags(sys))
                
    for group in dashboard_groups.values():
        group['search_tags_str'] = " ".join(group['search_tags'])
        group['system_variations'] = list(group['system_variations_map'].values())
        group['geom_by_system'] = geometric_mean_by_system_across_arguments(group['runs'])
        
    grouped_benchmarks = list(dashboard_groups.values())
        
    return render_template('dashboard.html', grouped_systems=grouped_systems, grouped_benchmarks=grouped_benchmarks)


@bp.route('/upload', methods=['GET', 'POST'])
def upload_benchmarks():
    if request.method == 'GET':
        unique_values = get_unique_field_values()
        
        # Build deduplicated list of historical system profiles for auto-fill dropdown
        all_systems = System.query.all()
        unique_profiles_map = {}
        for sys in all_systems:
            label = format_system_profile_label(sys)
            if label not in unique_profiles_map:
                profile_data = {
                    'label': label,
                    'primary_system_name': sys.primary_system_name or '',
                    'serial_number': sys.serial_number or '',
                    'chassis_version': sys.chassis_version or '',
                    'cooler_model': sys.cooler_model or '',
                    'psu_model': sys.psu_model or '',
                    'psu_wattage': sys.psu_wattage or '',
                    'custom_hardware': sys.custom_hardware or '',
                    'manual_notes': sys.manual_notes or '',
                    'external_off': sys.external_off,
                    'gpu_fans': sys.gpu_fans,
                    'memory_fans': sys.memory_fans,
                    'nvme_fans': sys.nvme_fans
                }
                unique_profiles_map[label] = profile_data
        
        existing_profiles = sorted(list(unique_profiles_map.values()), key=lambda x: x['label'])
        existing_profiles_json = json.dumps(existing_profiles)
        
        return render_template('upload.html', unique_values=unique_values, existing_profiles_json=existing_profiles_json)
        
    system_profile = build_system_profile_from_form(request.form)
    files = request.files.getlist('benchmark_files')
    
    if not files or files[0].filename == '':
        flash('No files selected for upload.', 'error')
        return redirect(url_for('pages.upload_benchmarks'))
        
    # Process files in a temporary directory
    temp_dir = tempfile.mkdtemp()
    extracted_xml_count = 0
    pop_import_notes()
    
    try:
        for f in files:
            filename = secure_filename(f.filename)
            file_path = os.path.join(temp_dir, filename)
            f.save(file_path)
            
            if filename.lower().endswith('.zip'):
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                    for info in zip_ref.infolist():
                        fpath = os.path.join(temp_dir, info.filename)
                        if info.filename.lower().endswith('.xml'):
                            parse_file(fpath, system_profile=system_profile)
                            extracted_xml_count += 1
                        elif is_sidecar_filename(info.filename):
                            _import_sidecar_json(fpath)
            elif filename.lower().endswith('.xml'):
                parse_file(file_path, system_profile=system_profile)
                extracted_xml_count += 1
            elif is_sidecar_filename(filename):
                _import_sidecar_json(file_path)
                
        for system in System.query.all():
            _, changed = sync_nvme_configs(system)
            if changed:
                db.session.flush()
        for system in System.query.all():
            auto_populate_hardware_spec(system)

        db.session.commit()
        
        # Run insights rebuild out-of-process (low priority) so the web UI stays responsive.
        if not schedule_insights_rebuild():
            def run_analysis_with_context(app_context):
                with app_context:
                    try:
                        with insights_rebuild_lock(block=False) as acquired:
                            if not acquired:
                                print("Insights rebuild already in progress; upload will be picked up by the next scheduled run.")
                                return
                            analyze_benchmarks()
                            analyze_ml_profiles()
                    except Exception as e:
                        print(f"Error in background benchmark analysis: {e}")
                    finally:
                        db.session.remove()

            threading.Thread(target=run_analysis_with_context, args=(current_app.app_context(),), daemon=True).start()
        
        if extracted_xml_count > 0:
            flash(f'Successfully ingested {extracted_xml_count} benchmark records.', 'success')
            seen_notes = set()
            for note in pop_import_notes():
                if note in seen_notes:
                    continue
                seen_notes.add(note)
                flash(note, 'success')
            shutil.rmtree(temp_dir, ignore_errors=True)
            return redirect(url_for('pages.dashboard'))
        else:
            flash('No valid XML benchmark files were found in the upload.', 'error')
            
    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred during processing: {str(e)}', 'error')
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        
    return redirect(url_for('pages.upload_benchmarks'))


@bp.route('/system/<int:system_id>/hardware_spec')
def hardware_spec_edit(system_id: int):
    system = SystemRepository.get_by_id_or_404(system_id)
    spec = HardwareSpec.query.filter_by(system_id=system.id).first()
    if spec is None:
        spec = HardwareSpec(system_id=system.id, source='auto')
        db.session.add(spec)
        db.session.flush()
    hints = missing_spec_hints(spec)
    schemas = SpecFieldSchema.query.order_by(
        SpecFieldSchema.blob_column, SpecFieldSchema.sort_order,
    ).all()
    return render_template(
        'hardware_spec.html',
        system=system,
        spec=spec,
        hints=hints,
        schemas=schemas,
    )


@bp.route('/hardware_spec/schemas')
def hardware_spec_schemas():
    """Manage the spec field schema definitions."""
    fields = SpecFieldSchema.query.order_by(
        SpecFieldSchema.blob_column, SpecFieldSchema.sort_order,
    ).all()
    return render_template('spec_schemas.html', fields=fields)


@bp.route('/system/<int:id>')
def system_detail(id):
    system = SystemRepository.get_by_id_or_404(id)
    _, nvme_changed = sync_nvme_configs(system)
    if nvme_changed:
        db.session.commit()
    
    unique_values = get_unique_field_values()
    
    # Group results by title and app_version.
    # Exclude perf counters (BAR_GRAPH that are marked non-primary).
    grouped_results = {}
    for result in system.results:
        b = result.benchmark
        if b and b.display_format == 'BAR_GRAPH' and not b.is_primary:
            continue
        id_str = f" [{b.identifier}]" if b.identifier else ""
        key = f"{b.title} ({b.app_version}){id_str}"
        if key not in grouped_results:
            grouped_results[key] = {
                'title': b.title,
                'app_version': b.app_version,
                'identifier': b.identifier,
                'description': b.description,
                'runs': []
            }
        grouped_results[key]['runs'].append(result)
        
    # Extract all known base arguments (BAR_GRAPH runs) for this benchmark group
    for key, group in grouped_results.items():
        base_args_list = [r.arguments or "" for r in group['runs'] if r.benchmark.display_format == 'BAR_GRAPH']
        
        # Sort runs so that base metrics appear before their associated sensors,
        # grouped by the base parameter configuration.
        def sort_key(run):
            args = run.arguments or ""
            b = run.benchmark
            is_sensor = b.display_format == 'LINE_GRAPH'
            
            base_args = args
            sensor_type = ""
            
            if is_sensor:
                # Dynamically find the longest base_arg that this sensor's arguments end with.
                # E.g. "GPU Power Consumption -width 1920 -height 1080 -opengl" ends with "-width 1920 -height 1080 -opengl"
                best_match = ""
                for ba in base_args_list:
                    if args.endswith(ba) and len(ba) > len(best_match):
                        best_match = ba
                        
                if best_match:
                    base_args = best_match
                    # The sensor type is whatever is before the base_args
                    sensor_type = args[:-len(best_match)].strip()
                else:
                    # Fallback if no exact suffix match is found
                    base_args = "zzz_unmatched"
                    sensor_type = args
                    
            # Primary sort: Base parameter string (groups the actual test parameters together)
            # Secondary sort: 0 if BAR_GRAPH (primary metric), 1 if LINE_GRAPH (sensor)
            # Tertiary sort: If it's a BAR_GRAPH, sort alphabetically by scale to group identical metrics.
            # Quaternary sort: Sensor type name (frequency, temp, usage)
            return (base_args, int(is_sensor), str(b.scale) if not is_sensor else "", sensor_type)
            
        group['runs'].sort(key=sort_key)

        by_arg = defaultdict(list)
        rep_scale = None
        for r in group['runs']:
            b = r.benchmark
            if b.display_format != 'BAR_GRAPH' or not b.is_primary:
                continue
            if r.value is None:
                continue
            try:
                v = float(r.value)
            except (TypeError, ValueError):
                continue
            if v <= 0 or math.isnan(v) or math.isinf(v):
                continue
            by_arg[r.arguments or ""].append(v)
            if rep_scale is None:
                rep_scale = b.scale
        per_cfg_means = [statistics.mean(vs) for vs in by_arg.values() if vs]
        group['geometric_mean_across_configs'] = geometric_mean_positive(per_cfg_means)
        group['geometric_mean_n_configs'] = len(per_cfg_means)
        group['geometric_mean_scale'] = rep_scale or ""

    grouped_list = list(grouped_results.values())
    hardware_components = split_component_list(system.hardware)
    software_components = split_component_list(system.software)
    system.profile_label = format_system_profile_label(system)
    system.primary_group_name = get_primary_group_name(system)
        
    return render_template(
        'system.html',
        system=system,
        grouped_results=grouped_list,
        hardware_components=hardware_components,
        software_components=software_components,
        unique_values=unique_values,
    )


@bp.route('/update_system/<int:id>', methods=['POST'])
def update_system(id):
    system = SystemRepository.get_by_id_or_404(id)
    system.identifier = clean_text(request.form.get('identifier')) or system.identifier
    new_primary_name = clean_text(request.form.get('primary_system_name')) or system.identifier
    old_primary_name = system.primary_system_name
    system.primary_system_name = new_primary_name
    system.serial_number = clean_text(request.form.get('serial_number'))
    system.chassis_version = clean_text(request.form.get('chassis_version'))
    system.cooler_model = clean_text(request.form.get('cooler_model'))
    system.psu_model = clean_text(request.form.get('psu_model'))
    system.psu_wattage = clean_text(request.form.get('psu_wattage'))
    system.custom_hardware = clean_text(request.form.get('custom_hardware'))
    system.manual_notes = clean_text(request.form.get('manual_notes'))
    system.hardware = clean_text(request.form.get('hardware')) or system.hardware
    system.software = clean_text(request.form.get('software')) or system.software
    system.external_off = checkbox_value(request.form, 'external_off')
    system.gpu_fans = checkbox_value(request.form, 'gpu_fans')
    system.memory_fans = checkbox_value(request.form, 'memory_fans')
    system.nvme_fans = checkbox_value(request.form, 'nvme_fans')

    db.session.flush()
    _, _ = sync_nvme_configs(system)

    for nvme_config in system.nvme_configs:
        nvme_config.slot_name = clean_text(request.form.get(f'nvme_slot_name_{nvme_config.id}')) or nvme_config.slot_name
        nvme_config.detected_name = clean_text(request.form.get(f'nvme_detected_name_{nvme_config.id}')) or nvme_config.detected_name
        nvme_config.notes = clean_text(request.form.get(f'nvme_notes_{nvme_config.id}'))
        nvme_config.top_thermal_pad = checkbox_value(request.form, f'nvme_top_thermal_pad_{nvme_config.id}')
        nvme_config.bottom_thermal_pad = checkbox_value(request.form, f'nvme_bottom_thermal_pad_{nvme_config.id}')

    db.session.commit()

    if old_primary_name != new_primary_name:
        _reconcile_primary_name_conflict(new_primary_name)
        db.session.commit()

    # The system may have been merged into another record; resolve to the current one.
    system = System.query.filter_by(primary_system_name=new_primary_name).first() or system
    flash('System profile updated.', 'success')
    return redirect(url_for('pages.system_detail', id=system.id))


@bp.route('/delete_system/<int:id>', methods=['POST'])
def delete_system(id):
    system = SystemRepository.get_by_id_or_404(id)
    system_name = system.identifier
    db.session.delete(system)
    delete_orphan_benchmarks()
    db.session.commit()
    flash(f'System "{system_name}" successfully deleted.', 'success')
    return redirect(url_for('pages.dashboard'))


@bp.route('/system/<int:system_id>/delete_benchmark', methods=['POST'])
def delete_system_benchmark(system_id):
    system = SystemRepository.get_by_id_or_404(system_id)
    title = clean_text(request.form.get('title'))
    app_version = clean_text(request.form.get('app_version'))
    identifier = clean_text(request.form.get('identifier'))

    if not title:
        flash('Missing benchmark title.', 'error')
        return redirect(url_for('pages.system_detail', id=system.id))

    deleted = delete_system_benchmark_suite(
        system.id,
        title=title,
        app_version=app_version or '',
        identifier=identifier or '',
    )
    db.session.commit()

    if deleted:
        flash(f'Removed {deleted} result(s) for "{title}" from this system.', 'success')
    else:
        flash(f'No results found for "{title}" on this system.', 'error')
    return redirect(url_for('pages.system_detail', id=system.id))


@bp.route('/compare')
def compare():
    systems_raw = System.query.all()
    grouped_systems = group_system_profiles(systems_raw)
    compare_system_groups_json = json.dumps(serialize_compare_system_groups(grouped_systems))
    return render_template(
        'compare.html',
        grouped_systems=grouped_systems,
        compare_system_groups_json=compare_system_groups_json,
        compare_by_options=COMPARE_BY_OPTIONS,
    )


@bp.route('/compare/s/<string:comp_id>')
def compare_saved(comp_id):
    """Render compare page; frontend will fetch the saved comparison payload."""
    systems_raw = System.query.all()
    grouped_systems = group_system_profiles(systems_raw)
    compare_system_groups_json = json.dumps(serialize_compare_system_groups(grouped_systems))
    return render_template(
        'compare.html',
        grouped_systems=grouped_systems,
        compare_system_groups_json=compare_system_groups_json,
        compare_by_options=COMPARE_BY_OPTIONS,
        saved_comp_id=comp_id,
    )


@bp.route('/compare/saved')
def list_saved_comparisons():
    """List recent saved comparisons with basic summary info."""
    rows = (
        SavedComparison.query
        .order_by(SavedComparison.created_at.desc())
        .limit(100)
        .all()
    )
    summaries = []
    for row in rows:
        payload = row.payload_json or {}
        systems = payload.get('systems') or []
        benchmarks = payload.get('benchmarks') or []
        system_labels = [s.get('shortName') or s.get('label') or str(s.get('id')) for s in systems]
        bench_labels = [b.get('label') or str(b.get('id')) for b in benchmarks]
        summaries.append({
            'id': row.id,
            'created_at': row.created_at,
            'systems': system_labels,
            'benchmarks': bench_labels,
        })
    return render_template('saved_comparisons.html', comparisons=summaries)


@bp.route('/compare/saved/<string:comp_id>/delete', methods=['POST'])
def delete_saved_comparison(comp_id):
    saved = SavedComparison.query.get(comp_id)
    if not saved:
        flash('Saved comparison not found.', 'error')
        return redirect(url_for('pages.list_saved_comparisons'))
    db.session.delete(saved)
    db.session.commit()
    flash('Saved comparison deleted.', 'success')
    return redirect(url_for('pages.list_saved_comparisons'))


@bp.route('/discriminating_benchmarks')
def discriminating_benchmarks():
    return render_template('discriminating_benchmarks.html')


@bp.route('/hardware_ranking')
def hardware_ranking():
    benchmarks = list_rankable_benchmarks()
    return render_template('hardware_ranking.html', benchmarks=benchmarks)


@bp.route('/insights')
def insights():
    # Exclude analysis rows that correspond only to non-primary BAR_GRAPH metrics (e.g. perf counters).
    primary_pairs = set(
        (b.title, b.app_version or '')
        for b in BenchmarkRepository.find_all_primary()
    )
    analyses = [
        a for a in BenchmarkAnalysis.query.order_by(BenchmarkAnalysis.benchmark_title, BenchmarkAnalysis.benchmark_app_version).all()
        if (a.benchmark_title, a.benchmark_app_version or '') in primary_pairs
    ]
    # Return as an array of structured mappings for the Jinja template
    return render_template('insights.html', analyses=analyses)
