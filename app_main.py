from app import create_app, db
from app.models import (
    System,
    Benchmark,
    BenchmarkResult,
    SystemNvmeConfig,
    BenchmarkAnalysis,
    SavedComparison,
    HardwareTheoreticalRank,
)
from app.parser import parse_benchmark_files, parse_file, pop_import_notes
from app.benchmark_util import delete_orphan_benchmarks, delete_system_benchmark_suite
from app.analyzer import analyze_benchmarks
from app.ml.analyzer import analyze_ml_profiles
from app.components import get_system_components
from flask import render_template, request, redirect, url_for, flash, send_file, jsonify
from urllib.parse import unquote
import os
import datetime
import threading
import zipfile
import tempfile
import shutil
import statistics
import math
import json
from collections import defaultdict
import re
import click
from werkzeug.utils import secure_filename

app = create_app()
app.secret_key = 'super-secret-benchmark-key'

PROFILE_STRING_FIELDS = (
    'primary_system_name',
    'chassis_version',
    'custom_hardware',
    'cooler_model',
    'psu_model',
    'psu_wattage',
    'manual_notes',
)

PROFILE_BOOL_FIELDS = (
    'external_off',
    'gpu_fans',
    'memory_fans',
    'nvme_fans',
)

def clean_text(value):
    return (value or '').strip()


def geometric_mean_positive(values):
    """
    Geometric mean for strictly positive finite samples.
    Returns None if empty or any non-positive value is included after filtering.
    """
    xs = []
    for x in values:
        if x is None:
            continue
        try:
            xf = float(x)
        except (TypeError, ValueError):
            return None
        if xf <= 0 or math.isnan(xf) or math.isinf(xf):
            return None
        xs.append(xf)
    if not xs:
        return None
    try:
        return statistics.geometric_mean(xs)
    except statistics.StatisticsError:
        return None


def geometric_mean_by_system_across_arguments(benchmark_rows):
    """
    For a group of Benchmark ORM rows (same suite), compute per-system geometric mean
    across distinct argument strings. Repeated results for the same (system, args)
    are averaged first, then geometric mean is taken across argument groups.
    """
    by_sys_then_args = defaultdict(lambda: defaultdict(list))
    scale = None
    proportion = None
    for bm in benchmark_rows:
        if scale is None:
            scale = bm.scale
        if proportion is None:
            proportion = bm.proportion
        for res in bm.results:
            if res.value is None:
                continue
            try:
                v = float(res.value)
            except (TypeError, ValueError):
                continue
            if v <= 0 or math.isnan(v) or math.isinf(v):
                continue
            arg = res.arguments or ""
            by_sys_then_args[res.system_id][arg].append(v)

    out = {}
    for sid, by_arg in by_sys_then_args.items():
        per_cfg_means = [statistics.mean(vs) for vs in by_arg.values() if vs]
        if not per_cfg_means:
            continue
        gm = geometric_mean_positive(per_cfg_means)
        if gm is None:
            continue
        out[sid] = {
            "geometric_mean": gm,
            "n_configs": len(per_cfg_means),
            "scale": scale or "",
            "proportion": proportion or "",
        }
    return out


def get_unique_field_values():
    unique_values = {}
    for field in PROFILE_STRING_FIELDS:
        if field == 'manual_notes':
            continue
        column = getattr(System, field)
        values = db.session.query(column).distinct().filter(column.isnot(None), column != '').order_by(column).all()
        unique_values[field] = sorted(list(set(v[0].strip() for v in values if v[0] and v[0].strip())))
    return unique_values

def checkbox_value(form, key):
    return form.get(key) in {'on', 'true', '1', 'yes'}

def build_system_profile_from_form(form):
    profile = {field: clean_text(form.get(field)) for field in PROFILE_STRING_FIELDS}
    for field in PROFILE_BOOL_FIELDS:
        profile[field] = checkbox_value(form, field)
    return profile

def split_component_list(value):
    return [item.strip() for item in (value or '').split(', ') if item.strip()]

def extract_storage_drives(hardware_text):
    for component in split_component_list(hardware_text):
        if component.startswith('Disk:'):
            disk_blob = component.split(':', 1)[1].strip()
            return [entry.strip() for entry in disk_blob.split(' + ') if entry.strip()]
    return []

def sync_nvme_configs(system):
    detected_drives = extract_storage_drives(system.hardware)
    existing_by_name = {config.detected_name: config for config in system.nvme_configs if config.detected_name}
    changed = False

    for index, drive_name in enumerate(detected_drives, start=1):
        config = existing_by_name.get(drive_name)
        if not config:
            config = SystemNvmeConfig(system=system, detected_name=drive_name)
            db.session.add(config)
            changed = True

        slot_name = config.slot_name or f"Drive {index}"
        if config.slot_name != slot_name:
            config.slot_name = slot_name
            changed = True
        if config.detected_name != drive_name:
            config.detected_name = drive_name
            changed = True

    return detected_drives, changed

def get_primary_group_name(system):
    return system.primary_system_name or system.identifier

def get_profile_badges(system):
    badges = []
    if system.chassis_version:
        badges.append(system.chassis_version)
    if system.cooler_model:
        badges.append(system.cooler_model)
    if system.psu_model or system.psu_wattage:
        psu_label = " ".join(part for part in [system.psu_wattage, system.psu_model] if part)
        badges.append(psu_label)
    if system.external_off:
        badges.append("External Off")

    fan_labels = []
    if system.gpu_fans:
        fan_labels.append("GPU Fans")
    if system.memory_fans:
        fan_labels.append("Memory Fans")
    if system.nvme_fans:
        fan_labels.append("NVMe Fans")
    if fan_labels:
        badges.append(", ".join(fan_labels))

    if system.custom_hardware:
        badges.append(system.custom_hardware)

    return badges

def format_system_profile_label(system):
    base_name = system.identifier
    badges = get_profile_badges(system)
    if not badges:
        return base_name
    return f"{base_name} | {' | '.join(badges)}"

def get_system_search_tags(system):
    tags = {
        clean_text(system.identifier).lower(),
        clean_text(get_primary_group_name(system)).lower(),
        clean_text(system.hardware).lower(),
        clean_text(system.software).lower(),
        clean_text(system.chassis_version).lower(),
        clean_text(system.cooler_model).lower(),
        clean_text(system.psu_model).lower(),
        clean_text(system.psu_wattage).lower(),
        clean_text(system.manual_notes).lower(),
        clean_text(system.custom_hardware).lower(),
    }
    if system.external_off:
        tags.add('external off')
    if system.gpu_fans:
        tags.add('gpu fans')
    if system.memory_fans:
        tags.add('memory fans')
    if system.nvme_fans:
        tags.add('nvme fans')
    return {tag for tag in tags if tag}

@app.route('/')
def dashboard():
    removed_orphans = delete_orphan_benchmarks()
    if removed_orphans:
        db.session.commit()

    systems_raw = System.query.all()
    
    # Group systems by the primary system family name and keep profile variations underneath.
    grouped_systems_dict = {}
    for sys in systems_raw:
        sys.primary_group_name = get_primary_group_name(sys)
        sys.profile_label = format_system_profile_label(sys)
        sys.search_tags = get_system_search_tags(sys)

        if sys.primary_group_name not in grouped_systems_dict:
            grouped_systems_dict[sys.primary_group_name] = {
                'group_name': sys.primary_group_name,
                'profiles': [],
                'search_tags': set()
            }
            
        group = grouped_systems_dict[sys.primary_group_name]
        group['profiles'].append(sys)
        group['search_tags'].update(sys.search_tags)
            
    for group in grouped_systems_dict.values():
        group['search_tags_str'] = " ".join(group['search_tags'])
        
    grouped_systems = list(grouped_systems_dict.values())

    # Perf counters are stored as BAR_GRAPH benchmarks too, but marked non-primary.
    # We exclude them from the dashboard "benchmarks" listing for clarity.
    primary_benchmarks = Benchmark.query.filter(
        Benchmark.display_format == 'BAR_GRAPH',
        Benchmark.is_primary.is_(True),
        Benchmark.results.any(),
    ).all()
    
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

@app.route('/upload', methods=['GET', 'POST'])
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
        return redirect(url_for('upload_benchmarks'))
        
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
                        if info.filename.lower().endswith('.xml'):
                            parse_file(os.path.join(temp_dir, info.filename), system_profile=system_profile)
                            extracted_xml_count += 1
            elif filename.lower().endswith('.xml'):
                parse_file(file_path, system_profile=system_profile)
                extracted_xml_count += 1
                
        for system in System.query.all():
            _, changed = sync_nvme_configs(system)
            if changed:
                db.session.flush()

        db.session.commit()
        
        # Trigger background analysis in a new thread so we don't block the UI
        # We need to pass the app context to the thread so it can query the DB natively
        def run_analysis_with_context(app_context):
            with app_context:
                try:
                    analyze_benchmarks()
                    analyze_ml_profiles()
                except Exception as e:
                    print(f"Error in background benchmark analysis: {e}")
                finally:
                    db.session.remove()

        threading.Thread(target=run_analysis_with_context, args=(app.app_context(),), daemon=True).start()
        
        if extracted_xml_count > 0:
            flash(f'Successfully ingested {extracted_xml_count} benchmark records.', 'success')
            seen_notes = set()
            for note in pop_import_notes():
                if note in seen_notes:
                    continue
                seen_notes.add(note)
                flash(note, 'success')
            shutil.rmtree(temp_dir, ignore_errors=True)
            return redirect(url_for('dashboard'))
        else:
            flash('No valid XML benchmark files were found in the upload.', 'error')
            
    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred during processing: {str(e)}', 'error')
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        
    return redirect(url_for('upload_benchmarks'))

@app.route('/system/<int:id>')
def system_detail(id):
    system = System.query.get_or_404(id)
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

@app.route('/update_system/<int:id>', methods=['POST'])
def update_system(id):
    system = System.query.get_or_404(id)
    system.identifier = clean_text(request.form.get('identifier')) or system.identifier
    system.primary_system_name = clean_text(request.form.get('primary_system_name')) or system.identifier
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
    flash('System profile updated.', 'success')
    return redirect(url_for('system_detail', id=system.id))

@app.route('/delete_system/<int:id>', methods=['POST'])
def delete_system(id):
    system = System.query.get_or_404(id)
    system_name = system.identifier
    db.session.delete(system)
    delete_orphan_benchmarks()
    db.session.commit()
    flash(f'System "{system_name}" successfully deleted.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/system/<int:system_id>/delete_benchmark', methods=['POST'])
def delete_system_benchmark(system_id):
    system = System.query.get_or_404(system_id)
    title = clean_text(request.form.get('title'))
    app_version = clean_text(request.form.get('app_version'))
    identifier = clean_text(request.form.get('identifier'))

    if not title:
        flash('Missing benchmark title.', 'error')
        return redirect(url_for('system_detail', id=system.id))

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
    return redirect(url_for('system_detail', id=system.id))

# Ordered list of (key, label) for "Compare by" dropdown; key must match get_system_components() keys.
COMPARE_BY_OPTIONS = [
    ('system_name', 'System name'),
    ('identifier', 'System identifier'),
    ('processor', 'CPU (Processor)'),
    ('graphics', 'GPU (Graphics)'),
    ('memory', 'Memory'),
    ('motherboard', 'Motherboard'),
    ('chipset', 'Chipset'),
    ('os', 'Operating system'),
    ('kernel_version', 'Kernel version'),
    ('nvidia_driver', 'NVIDIA driver version'),
    ('mesa_version', 'Mesa version'),
    ('llvm_version', 'LLVM version'),
    ('vulkan_driver', 'Vulkan driver'),
    ('chassis_version', 'Chassis version'),
    ('cooler_model', 'Cooler'),
    ('psu', 'PSU'),
    ('custom_hardware', 'Custom hardware'),
    ('external_off', 'External off'),
    ('gpu_fans', 'GPU fans'),
    ('memory_fans', 'Memory fans'),
    ('nvme_fans', 'NVMe fans'),
    ('thermal_pad_above_nvme', 'Thermal pad above NVMe'),
    ('thermal_pad_below_nvme', 'Thermal pad below NVMe'),
    ('thermal_pad_sandwich_nvme', 'Thermal pad sandwich NVMe'),
]

# Hint substrings for CPU-bound workloads (used to scope leaderboard features).
_CPU_BENCHMARK_HINTS = (
    "stockfish", "chess",
    "7-zip", "7zip",
    "compression", "decompression",
    "openssl",
    "ffmpeg", "x264", "x265", "handbrake", "encoding", "transcod",
    "coremark", "pybench", "phpbench",
    "compilebench",
    "dav1d", "rav1e", "svt-av1",
    "blosc", "lz4", "zstd",
    "redis", "memcached",
    "sqlite",
)

# NVMe chassis layout toggles: often aligned with system type and replicate across
# machines; they are misleading on CPU/GPU workloads unless the test is storage-class.
NVME_LAYOUT_LEADERBOARD_KEYS = frozenset({
    "thermal_pad_above_nvme",
    "thermal_pad_below_nvme",
    "thermal_pad_sandwich_nvme",
    "nvme_fans",
})

INSIGHT_CPU_SCOPED_KEYS = frozenset({
    "processor",
    "memory",
    "motherboard",
    "chipset",
    "os",
    "kernel_version",
    "llvm_version",
    "cooler_model",
    "chassis_version",
    "psu",
    "custom_hardware",
    "external_off",
    "memory_fans",
})
INSIGHT_GPU_SCOPED_KEYS = frozenset({
    "graphics",
    "nvidia_driver",
    "mesa_version",
    "llvm_version",
    "vulkan_driver",
    "processor",
    "memory",
    "os",
    "chassis_version",
    "gpu_fans",
})
INSIGHT_STORAGE_SCOPED_KEYS = frozenset({
    "nvme_fans",
    "thermal_pad_above_nvme",
    "thermal_pad_below_nvme",
    "thermal_pad_sandwich_nvme",
    "custom_hardware",
    "chassis_version",
    "external_off",
    "psu",
    "memory",
    "processor",
})


def _insights_infer_scope(text_blob: str) -> str:
    scope = "general"
    if ("kernel" in text_blob and ("build" in text_blob or "compil" in text_blob or "make" in text_blob
         or "gcc" in text_blob or "clang" in text_blob)) or ("compil" in text_blob and "linux" in text_blob):
        scope = "cpu"
    elif any(h in text_blob for h in _CPU_BENCHMARK_HINTS):
        scope = "cpu"
    elif any(k in text_blob for k in ["vulkan", "cuda", "opengl", "render", "graphics", "gpu "]):
        scope = "gpu"
    elif any(k in text_blob for k in ["nvme", "disk", "io", "i/o", "storage", "ssd", "hdd", "throughput",
                                        "fio", "postmark", "iometer"]):
        scope = "storage"
    return scope


def _insights_workload_context_from_analysis(
    title: str, app_version: str, args_str: str, text_blob: str,
) -> dict:
    """Prefer perf/sensor workload profile from BenchmarkAnalysis when available."""
    from app.workload_profile import workload_context_for_insights

    records = BenchmarkAnalysis.query.filter_by(
        benchmark_title=title,
        benchmark_app_version=app_version or "",
    ).all()
    analysis_json = records[0].analysis_json if records else None
    args_key = "default" if (not args_str or args_str.lower() == "default") else args_str
    return workload_context_for_insights(title, app_version, args_key, analysis_json, text_blob)


def _insights_scope_from_analysis(title: str, app_version: str, args_str: str, text_blob: str) -> str:
    return _insights_workload_context_from_analysis(title, app_version, args_str, text_blob)["scope"]


def _insights_allowed_singles_for_scope(
    scope: str,
    include_all_component_keys: bool,
    active_bottlenecks: list[str] | None = None,
):
    from app.analyzer import INSIGHT_COMPONENT_KEYS
    from app.workload_profile import SCOPE_HARDWARE_KEYS
    if active_bottlenecks and len(active_bottlenecks) >= 2:
        allowed = set()
        for bottleneck in active_bottlenecks:
            allowed |= SCOPE_HARDWARE_KEYS.get(bottleneck, frozenset())
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k in allowed]
    elif scope in SCOPE_HARDWARE_KEYS and scope != "general":
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k in SCOPE_HARDWARE_KEYS[scope]]
    elif include_all_component_keys:
        allowed_singles = list(INSIGHT_COMPONENT_KEYS)
    else:
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k not in NVME_LAYOUT_LEADERBOARD_KEYS]
    if not allowed_singles:
        allowed_singles = list(INSIGHT_COMPONENT_KEYS)
    return allowed_singles


def _load_primary_insights_bundle(title, app_version, args_str, scope_override=""):
    """
    Load primary BAR_GRAPH scores and component maps for a benchmark/config.
    Returns (bundle_dict, None) or (None, (error_message, http_code)).
    """
    from app.analyzer import MIN_SYSTEMS_TOTAL

    title = (title or "").strip()
    app_version = (app_version or "").strip()
    args_str = (args_str or "").strip()
    scope_override = (scope_override or "").strip().lower()

    if not title:
        return None, ("Missing benchmark_title query parameter", 400)

    bms_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(True),
    )
    if app_version:
        bms_q = bms_q.filter(Benchmark.app_version == app_version)
    primary_bms = bms_q.all()
    if not primary_bms:
        return None, ("No primary BAR_GRAPH benchmark found for the given title/app_version", 404)

    rep_bm = primary_bms[0]
    label_map = dict(COMPARE_BY_OPTIONS)

    def proportion_is_lower_better(p):
        p = (p or "").strip().upper()
        if p == "LIB":
            return True
        if p == "HIB":
            return False
        pl = (p or "").lower()
        return "lower" in pl and "better" in pl

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    args_analysis_key = "default" if (not args_str or args_str.lower() == "default") else args_str
    args_db = "" if args_analysis_key == "default" else args_str

    primary_bm_ids = [b.id for b in primary_bms]
    all_results = BenchmarkResult.query.filter(
        BenchmarkResult.benchmark_id.in_(primary_bm_ids),
        BenchmarkResult.arguments == args_db,
        BenchmarkResult.value.isnot(None),
    ).all()

    if not all_results:
        return None, ("No BAR_GRAPH results for this benchmark/config", 404)

    by_system_vals = defaultdict(list)
    for r in all_results:
        by_system_vals[r.system_id].append(r.value)

    y_norm_by_system = {}
    y_raw_by_system = {}
    for sid, vals in by_system_vals.items():
        y_raw = statistics.mean([v for v in vals if v is not None])
        y_raw_by_system[sid] = y_raw
        y_norm_by_system[sid] = y_raw * y_flip

    sys_ids = sorted(y_raw_by_system.keys())
    systems = System.query.filter(System.id.in_(sys_ids)).all()
    systems_by_id = {s.id: s for s in systems}
    comps_by_sid = {s.id: get_system_components(s) for s in systems}

    text_blob = " ".join([
        (rep_bm.title or ""),
        (rep_bm.description or ""),
        args_str or "",
    ]).lower()
    wl_ctx = _insights_workload_context_from_analysis(title, app_version, args_analysis_key, text_blob)
    scope = wl_ctx["scope"]
    active_bottlenecks = list(wl_ctx.get("active_bottlenecks") or [])
    if scope == "general":
        scope = _insights_infer_scope(text_blob)
        active_bottlenecks = [scope] if scope in {"cpu", "gpu", "storage", "memory"} else []
    include_all_component_keys = scope_override == "all"
    if scope_override in {"all", "general"}:
        scope = "general"
        active_bottlenecks = []
    elif scope_override in {"cpu", "gpu", "storage", "memory"}:
        scope = scope_override
        active_bottlenecks = [scope_override]
    allowed_singles = _insights_allowed_singles_for_scope(
        scope, include_all_component_keys, active_bottlenecks or None,
    )

    bundle = {
        "MIN_SYSTEMS_TOTAL": MIN_SYSTEMS_TOTAL,
        "rep_bm": rep_bm,
        "primary_bms": primary_bms,
        "primary_bm_ids": primary_bm_ids,
        "label_map": label_map,
        "y_label_base": rep_bm.scale or "Score",
        "is_lower_better": is_lower_better,
        "y_flip": y_flip,
        "args_analysis_key": args_analysis_key,
        "args_db": args_db,
        "y_raw_by_system": y_raw_by_system,
        "y_norm_by_system": y_norm_by_system,
        "sys_ids": sys_ids,
        "systems_by_id": systems_by_id,
        "comps_by_sid": comps_by_sid,
        "scope": scope,
        "allowed_singles": allowed_singles,
        "title": title,
        "app_version": app_version,
    }
    return bundle, None


def _insights_signal_to_noise_raw(buckets_sid, y_raw_by_system):
    """
    Ratio: spread of cohort means / median within-cohort stdev (raw benchmark units).
    High values => cohort centroids differ more than typical scatter inside a cohort.
    """
    cohort_means = []
    inner_stds = []
    for sids in buckets_sid.values():
        ys = [y_raw_by_system[sid] for sid in sids if sid in y_raw_by_system]
        if not ys:
            continue
        cohort_means.append(statistics.mean(ys))
        if len(ys) > 1:
            inner_stds.append(statistics.stdev(ys))
    if len(cohort_means) < 2:
        return 0.0, 0.0
    spread = max(cohort_means) - min(cohort_means)
    med_inner = statistics.median(inner_stds) if inner_stds else 0.0
    sn = spread / (med_inner + 1e-9)
    return float(sn), float(spread)


def _insights_alignment_tier(eta_sq, sn_ratio):
    """
    Heuristic label for whether scores track this component split vs looking noise-like.
    Not causal — association only, with replication gates applied upstream.
    """
    eta_sq = float(eta_sq)
    sn_ratio = float(sn_ratio)
    if eta_sq >= 0.55 or sn_ratio >= 5.0:
        return (
            "strong",
            "Scores line up distinctly across these component values versus within-cohort scatter.",
        )
    if eta_sq >= 0.28 or sn_ratio >= 2.5:
        return (
            "moderate",
            "Meaningful-looking spread between cohorts; more data would firm up how much this part matters.",
        )
    return (
        "weak",
        "Alignment is limited: cohort differences are small relative to noise, or effects overlap a lot.",
    )


def _insights_alignment_rank_score(eta_sq, sn_ratio):
    """Order components by combined association strength (unitless, ~0–1)."""
    sn_term = min(1.0, float(sn_ratio) / 6.0)
    return 0.55 * float(eta_sq) + 0.45 * sn_term


def _insights_eta_squared_norm_buckets(value_to_y_norm_lists):
    vals = []
    for ys in value_to_y_norm_lists.values():
        vals.extend(ys)
    if len(vals) < 2:
        return 0.0
    grand_mean = statistics.mean(vals)
    ss_total = sum((y - grand_mean) ** 2 for y in vals)
    if ss_total < 1e-18:
        return 0.0
    ss_between = 0.0
    for ys in value_to_y_norm_lists.values():
        nj = len(ys)
        mj = statistics.mean(ys)
        ss_between += nj * (mj - grand_mean) ** 2
    return ss_between / ss_total


@app.route('/compare')
def compare():
    systems_raw = System.query.all()
    systems = []
    for sys in systems_raw:
        sys.primary_group_name = get_primary_group_name(sys)
        sys.profile_label = format_system_profile_label(sys)
        sys.components = get_system_components(sys)
        systems.append(sys)
    systems.sort(key=lambda s: s.identifier)
    return render_template('compare.html', systems=systems, compare_by_options=COMPARE_BY_OPTIONS)


@app.route('/compare/s/<string:comp_id>')
def compare_saved(comp_id):
    """Render compare page; frontend will fetch the saved comparison payload."""
    systems_raw = System.query.all()
    systems = []
    for sys in systems_raw:
        sys.primary_group_name = get_primary_group_name(sys)
        sys.profile_label = format_system_profile_label(sys)
        sys.components = get_system_components(sys)
        systems.append(sys)
    systems.sort(key=lambda s: s.identifier)
    return render_template('compare.html', systems=systems, compare_by_options=COMPARE_BY_OPTIONS, saved_comp_id=comp_id)


@app.route('/compare/saved')
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


@app.route('/compare/saved/<string:comp_id>/delete', methods=['POST'])
def delete_saved_comparison(comp_id):
    saved = SavedComparison.query.get(comp_id)
    if not saved:
        flash('Saved comparison not found.', 'error')
        return redirect(url_for('list_saved_comparisons'))
    db.session.delete(saved)
    db.session.commit()
    flash('Saved comparison deleted.', 'success')
    return redirect(url_for('list_saved_comparisons'))


def generate_comparison_id():
    # Short, URL-safe slug (16 hex chars is plenty)
    import secrets
    return secrets.token_hex(8)


@app.route('/export/slide', methods=['POST'])
def export_slide():
    """
    Receive a PNG data URL from the frontend, save it as a static file, and
    return JSON with URLs so the image can be viewed (and saved) from a normal
    <img> page.
    """
    data_url = request.form.get('image')
    if not data_url:
        return jsonify({"error": "Missing image payload"}), 400

    # Expect "data:image/png;base64,AAAA..."
    if ',' in data_url:
        _, b64data = data_url.split(',', 1)
    else:
        b64data = data_url

    import base64
    import secrets

    try:
        binary = base64.b64decode(b64data)
    except Exception:
        return jsonify({"error": "Invalid image payload"}), 400

    # Persist under static/exports so it can be served directly.
    exports_dir = os.path.join(app.static_folder, 'exports')
    os.makedirs(exports_dir, exist_ok=True)

    export_id = secrets.token_hex(8)
    filename = f'{export_id}.png'
    filepath = os.path.join(exports_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(binary)

    image_url = url_for('static', filename=f'exports/{filename}')
    view_url = url_for('view_export_slide', export_id=export_id)

    return jsonify({"id": export_id, "image_url": image_url, "view_url": view_url})


@app.route('/export/slide/<string:export_id>')
def view_export_slide(export_id):
    """Simple view that shows a saved export PNG inside an <img>."""
    filename = f'{export_id}.png'
    filepath = os.path.join(app.static_folder, 'exports', filename)
    if not os.path.exists(filepath):
        flash('Export not found.', 'error')
        return redirect(url_for('compare'))

    # Hint to Chrome users that Firefox handles slide downloads more reliably.
    ua = (request.user_agent.string or '').lower()
    if 'chrome' in ua and 'firefox' not in ua:
        flash('Note: exported slide downloads tend to work more reliably in Firefox than in Chrome.', 'info')
    image_url = url_for('static', filename=f'exports/{filename}')
    download_url = url_for('download_export_slide', export_id=export_id)
    return render_template('export_slide.html', image_url=image_url, export_id=export_id, download_url=download_url)


@app.route('/export/slide/<string:export_id>/delete', methods=['POST'])
def delete_export_slide(export_id):
    """Delete a previously saved export PNG."""
    filename = f'{export_id}.png'
    filepath = os.path.join(app.static_folder, 'exports', filename)
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            flash('Export deleted.', 'success')
        except OSError:
            flash('Failed to delete export.', 'error')
    else:
        flash('Export not found.', 'error')
    return redirect(url_for('list_export_slides'))


@app.route('/export/slide/<string:export_id>/download')
def download_export_slide(export_id):
    """Download an export PNG with a friendly filename."""
    filename = f'{export_id}.png'
    filepath = os.path.join(app.static_folder, 'exports', filename)
    if not os.path.exists(filepath):
        flash('Export not found.', 'error')
        return redirect(url_for('list_export_slides'))
    return send_file(
        filepath,
        mimetype='image/png',
        as_attachment=True,
        download_name='benchviz-comparison.png',
    )


@app.route('/export/slides')
def list_export_slides():
    """List saved export PNGs under static/exports."""
    exports_dir = os.path.join(app.static_folder, 'exports')
    exports = []
    if os.path.isdir(exports_dir):
        for name in sorted(os.listdir(exports_dir), reverse=True):
            if not name.lower().endswith('.png'):
                continue;
            export_id = os.path.splitext(name)[0]
            filepath = os.path.join(exports_dir, name)
            try:
                mtime = os.path.getmtime(filepath)
                created_at = datetime.datetime.fromtimestamp(mtime)
            except Exception:
                created_at = None
            exports.append({
                'id': export_id,
                'image_url': url_for('static', filename=f'exports/{name}'),
                'view_url': url_for('view_export_slide', export_id=export_id),
                'created_at': created_at,
            })
    exports.sort(key=lambda x: x['created_at'] or datetime.datetime.min, reverse=True)

    # Hint to Chrome users that Firefox handles slide downloads more reliably.
    ua = (request.user_agent.string or '').lower()
    if 'chrome' in ua and 'firefox' not in ua:
        flash('Note: exported slide downloads tend to work more reliably in Firefox than in Chrome.', 'info')

    return render_template('export_slides.html', exports=exports)


@app.route('/api/save_comparison', methods=['POST'])
def api_save_comparison():
    """Persist a comparison definition and return a short id."""
    try:
        payload = request.get_json(force=True)
    except Exception:
        return {"error": "Invalid JSON payload"}, 400

    if not isinstance(payload, dict):
        return {"error": "Payload must be an object"}, 400

    # Basic validation: require systems and benchmarks lists
    systems = payload.get('systems') or []
    benchmarks = payload.get('benchmarks') or []
    if not systems or not benchmarks:
        return {"error": "Payload must include non-empty 'systems' and 'benchmarks' arrays"}, 400

    comp_id = generate_comparison_id()
    saved = SavedComparison(id=comp_id, payload_json=payload)
    db.session.add(saved)
    db.session.commit()
    return {"id": comp_id}, 200


@app.route('/api/saved_comparison/<string:comp_id>')
def api_saved_comparison(comp_id):
    saved = SavedComparison.query.get(comp_id)
    if not saved:
        return {"error": "Comparison not found"}, 404
    return saved.payload_json, 200

@app.route('/api/compare')
def api_compare():
    system_ids = request.args.getlist('system_ids')
    config_params = request.args.getlist('config')
    benchmark_ids = request.args.getlist('benchmark_id')

    if not system_ids:
        return {"error": "Missing system_ids parameter(s)"}, 400

    # Build list of (benchmark_id, args_filter). args_filter None = all configs (first per system).
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
    from app.args_pooling import (
        extract_flag_values,
        parse_pool_flags,
        pool_key_for_args_by_flags,
    )

    pool_flags = parse_pool_flags(request.args.get('pool_arg_flags'))
    if pool_equivalent_configs and not pool_flags:
        # Pooling enabled but no flags specified -> no-op (falls back to normal compare).
        pool_equivalent_configs = False

    comparison_groups = []
    from app.ob_cache_sync import load_ob_cache_index
    from app.pts_compare import (
        build_pts_context_for_compare_group,
        build_pts_global_harmonic_summary,
        build_pts_global_summary,
        build_pts_ob_global_summary,
    )

    ob_index_cache = load_ob_cache_index()

    # When pooling equivalent configs, build a lookup of:
    # (benchmark_title, app_version, pool_key) -> set(raw_args_strings)
    pool_raw_args_map = defaultdict(set)  # only used when pool_equivalent_configs=True
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
            # Pivot to primary benchmark with same title+version (same as main loop).
            if not getattr(primary_benchmark, "is_primary", False):
                candidate = Benchmark.query.filter(
                    Benchmark.title == primary_benchmark.title,
                    Benchmark.app_version == primary_benchmark.app_version,
                    Benchmark.display_format == 'BAR_GRAPH',
                    Benchmark.is_primary == True,
                ).first()
                if candidate:
                    primary_benchmark = candidate
            pk = pool_key_for_args_by_flags(args_filter, pool_flags)
            if pk:
                pool_raw_args_map[(primary_benchmark.title, primary_benchmark.app_version, pk)].add(args_filter)

    pool_processed_keys = set()  # prevent duplicate comparison groups within a pool

    def _pooled_flag_suffix_from_args(args_text: str | None) -> str:
        """For pooled compare mode, label bars with the actual pooled flag value(s)."""
        if not args_text or not pool_flags:
            return ""
        vals = extract_flag_values(args_text, pool_flags)
        if not vals:
            return ""
        if len(vals) == 1:
            return str(vals[0])
        # Keep it compact; exact content is still readable in hover.
        return "/".join(str(v) for v in vals[:3])

    for b_id, args_filter in config_list:
        try:
            b_id = int(b_id)
        except (ValueError, TypeError):
            continue
        primary_benchmark = db.session.get(Benchmark, b_id)
        if not primary_benchmark:
            continue
        # If the selected benchmark id points at a non-primary BAR_GRAPH (e.g. a perf counter),
        # pivot to a primary benchmark with the same title+version so comparisons use the real
        # performance result.
        if not getattr(primary_benchmark, "is_primary", False):
            candidate = Benchmark.query.filter(
                Benchmark.title == primary_benchmark.title,
                Benchmark.app_version == primary_benchmark.app_version,
                Benchmark.display_format == 'BAR_GRAPH',
                Benchmark.is_primary == True,
            ).first()
            if candidate:
                primary_benchmark = candidate

        # Resolve benchmark IDs that actually have results for the selected systems and match
        # this test (title + app_version). Use results as source of truth so we never miss
        # benchmarks like Blender that may differ by identifier or is_primary across imports.
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

        if pool_equivalent_configs and args_filter is not None:
            current_base_key = pool_key_for_args_by_flags(args_filter, pool_flags) or str(args_filter)
            # Pooling is computed per (benchmark title+version) suite across all selected
            # configs so we can build axes like:
            #   --cycles-device HIP,CUDA   and   --cycles-device HIP,OPTIX
            #
            # We also keep common flag values (present on all selected systems) unpooled.
            suite_key = (primary_benchmark.title, primary_benchmark.app_version)
            suite_task_key = (suite_key[0], suite_key[1], "pool-axes", current_base_key)
            if suite_task_key in pool_processed_keys:
                continue
            pool_processed_keys.add(suite_task_key)
            pooling_active = True

            # Collect all selected raw args filters for this suite key (not just this one loop item).
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
                    cand = Benchmark.query.filter(
                        Benchmark.title == b2.title,
                        Benchmark.app_version == b2.app_version,
                        Benchmark.display_format == "BAR_GRAPH",
                        Benchmark.is_primary == True,
                    ).first()
                    if cand:
                        b2 = cand
                if getattr(b2, "title", None) == primary_benchmark.title and (b2.app_version or "") == (primary_benchmark.app_version or ""):
                    suite_raw_args_filters.append(str(args_filter2))

            # De-dupe while preserving order
            deduped = []
            seen_ra = set()
            for ra in suite_raw_args_filters:
                if ra in seen_ra:
                    continue
                seen_ra.add(ra)
                deduped.append(ra)
            suite_raw_args_filters = deduped

            # CRITICAL: only pool configs that share the same "base args" after
            # removing pooled flag values. This avoids merging different scenes/files
            # when user only requested pooling e.g. --cycles-device.
            suite_raw_args_filters = [
                ra for ra in suite_raw_args_filters
                if (pool_key_for_args_by_flags(ra, pool_flags) or ra) == current_base_key
            ]

            # Extract the pooled-flag "values" for each selected raw args string.
            # For now we treat "value" as the first extracted value.
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

            # If we didn't recognize any pool-flag values, fall back to normal behavior.
            if not raw_args_to_value:
                pooling_active = False
                args_list = [args_filter]
            else:
                all_raw_args = list(raw_args_to_value.keys())
                # Determine which (flag value) is present for which selected systems.
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

                # Common values: keep as separate axes using their original raw args string.
                for ra in suite_raw_args_filters:
                    v = raw_args_to_value.get(ra)
                    if not v:
                        continue
                    if v in common_values:
                        if ra not in axis_raw_args_map:
                            axis_args_list.append(ra)
                        axis_raw_args_map[ra] = [ra]

                # Non-common values: build overlapping independent pooled groups.
                def _compatible_with_group(v: str, group_values: list[str]) -> bool:
                    v_set = system_present_by_value.get(v, set())
                    for m in group_values:
                        m_set = system_present_by_value.get(m, set())
                        if v_set.intersection(m_set):
                            return False
                    return True

                # Candidate axis label uses the first pooled flag's "name".
                axis_flag_name = pool_flags[0].lstrip('-') if pool_flags else 'arg'

                seen_groups: set[frozenset[str]] = set()
                for pivot in non_common_values:
                    group = [pivot]
                    # Deterministic order: iterate non_common_values sorted.
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
                    # Collect all selected raw args strings whose pool value is in this group.
                    group_raw_args = [ra for ra in suite_raw_args_filters if raw_args_to_value.get(ra) in gset]
                    axis_raw_args_map[group_label] = group_raw_args
                    axis_args_list.append(group_label)

                # If somehow we have no axes (e.g. only common values), fall back.
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
            # Include None/empty arguments so options like "Unix Makefiles"
            # (which may not have an explicit arguments string) still appear
            # as separate configurations.
            args_list = [r[0] for r in distinct_rows]
            if not args_list:
                continue

            # If user selected "All configurations" and requested pooling, pool across all
            # discovered args for this benchmark suite.
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

                if raw_args_to_value:
                    pooling_active = True
                    axis_raw_args_map: dict[str, list[str]] = {}
                    axis_args_list: list[str] = []

                    # Partition by "base key" (args with pooled flag values removed).
                    base_to_raws: dict[str, list[str]] = defaultdict(list)
                    for ra in suite_raw_args_filters:
                        base = pool_key_for_args_by_flags(ra, pool_flags) or ra
                        base_to_raws[base].append(ra)

                    axis_flag_name = pool_flags[0].lstrip('-') if pool_flags else 'arg'
                    selected_sys_set = set(sys_id_ints)

                    for base_key, base_raws in base_to_raws.items():
                        base_raws = list(dict.fromkeys(base_raws))  # de-dupe preserve order
                        base_raw_to_value = {ra: raw_args_to_value.get(ra) for ra in base_raws if raw_args_to_value.get(ra)}
                        if not base_raw_to_value:
                            continue
                        base_values = list(dict.fromkeys(base_raw_to_value.values()))

                        q_all = BenchmarkResult.query.filter(
                            BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
                            BenchmarkResult.system_id.in_(sys_id_ints),
                            BenchmarkResult.arguments.in_(list(base_raw_to_value.keys())),
                        ).all()
                        system_present_by_value: dict[str, set[int]] = defaultdict(set)
                        for r in q_all:
                            v = base_raw_to_value.get(r.arguments)
                            if v:
                                system_present_by_value[v].add(r.system_id)

                        common_values = {v for v in base_values if system_present_by_value.get(v, set()) == selected_sys_set}
                        non_common_values = [v for v in base_values if v not in common_values]

                        # Keep common values unpooled (one axis each, raw args label).
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

        # Track non-empty primary arguments for this benchmark so we can
        # associate sensor runs even when the primary run's arguments are empty.
        nonempty_primary_args = []
        if not pooling_active:
            nonempty_primary_args = [
                a.strip() for a in args_list
                if isinstance(a, str) and a.strip()
            ]

        for args_val in args_list:
            charts = []
            sys_args_map = {}
            system_details = []
            primary_args_set = set()

            # Fetch all primary (BAR_GRAPH) results for this args_val so we get every
            # metric per run (e.g. FIO: MB/s and IOPS per option).
            q_prim = BenchmarkResult.query.filter(
                BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
                BenchmarkResult.system_id.in_(sys_id_ints),
            )
            if pooling_active:
                # args_val is a canonical pool key (display), but filtering must use
                # the raw args strings that were selected and mapped into this axis.
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
                q_prim = q_prim.filter(BenchmarkResult.arguments == args_val)
            all_prim_results = q_prim.all()

            # Group by benchmark_id so we get one chart per primary metric (e.g. MB/s, IOPS).
            by_bm_id = defaultdict(list)
            for r in all_prim_results:
                by_bm_id[r.benchmark_id].append(r)
                sys_args_map[r.system_id] = r.arguments
                if r.arguments:
                    primary_args_set.add(r.arguments.strip())

            for sys_id in sys_id_ints:
                if sys_id not in sys_args_map:
                    continue
                system = db.session.get(System, sys_id)
                if not system:
                    continue
                if not any(s['id'] == sys_id for s in system_details):
                    system_details.append({
                        'id': sys_id,
                        'short_name': system.identifier,
                        'full_label': format_system_profile_label(system)
                    })

            # Build one chart per primary benchmark that has results (e.g. MB/s and IOPS).
            primary_benchmarks = Benchmark.query.filter(
                Benchmark.id.in_(by_bm_id.keys())
            ).all()

            if pooling_active:
                # In pooling mode, "all_prim_results" can span multiple benchmark IDs
                # that are effectively fragments of one pooled axis (e.g. HIP-only bm id
                # and CUDA-only bm id). Build ONE synthetic chart per logical metric
                # signature (description/scale/proportion), not one total chart.
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

                # Rebuild sys_args_map from the first synthetic metric; sufficient to
                # map sensors to the same pooled run arguments.
                sys_args_map = {}
                first_sig = True

                for sig, sig_rows in sig_to_rows.items():
                    desc_sig, scale_sig, prop_sig, disp_sig = sig
                    prop = (prop_sig or "").strip().upper()
                    lower_better = prop == "LIB"
                    primary_traces = []
                    for sys_id in sys_id_ints:
                        candidates = [r for r in sig_rows if r.system_id == sys_id]
                        if not candidates:
                            continue
                        def score_key(r):
                            v = r.value
                            if v is None:
                                return float("inf") if lower_better else float("-inf")
                            return float(v)
                        res = min(candidates, key=score_key) if lower_better else max(candidates, key=score_key)
                        if first_sig:
                            sys_args_map[sys_id] = res.arguments
                        system = db.session.get(System, sys_id)
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
                        # Prefer description for disambiguating metrics like
                        # 7-Zip Compression vs Decompression (both scale=MIPS).
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
                # Keep primary charts in definition order (e.g. first in XML = first) so wins/losses use the first result.
                for bm in sorted(primary_benchmarks, key=lambda x: x.id):
                    results_for_bm = by_bm_id.get(bm.id, [])
                    if not results_for_bm:
                        continue
                    primary_traces = []
                    for sys_id in sys_id_ints:
                        res = next((r for r in results_for_bm if r.system_id == sys_id), None)
                        if not res:
                            continue
                        system = db.session.get(System, sys_id)
                        if not system:
                            continue
                        system_label = format_system_profile_label(system)
                        short_name = system.identifier
                        trace = {
                            "name": short_name,
                            "type": "bar" if bm.display_format == "BAR_GRAPH" else "scatter",
                            "customdata": [system_label],
                            "hovertemplate": "%{customdata[0]}<br>%{x}<extra></extra>" if bm.display_format == "BAR_GRAPH" else None
                        }
                        if bm.display_format == "BAR_GRAPH":
                            trace["x"] = [short_name]
                            trace["y"] = [res.value]
                        elif bm.display_format == "LINE_GRAPH":
                            y_data = res.data_json or []
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

            sensors = Benchmark.query.filter(
                Benchmark.title == primary_benchmark.title,
                Benchmark.app_version == primary_benchmark.app_version,
                Benchmark.display_format == 'LINE_GRAPH',
            ).all()
            # Only attach charts that are clearly sensor metrics (temp, freq, usage, power);
            # skip other LINE_GRAPH data (e.g. sample indices or raw timestamps) that would show wrong scale.
            sensor_keywords = (
                'temperature', 'frequency', 'usage', 'power', 'celsius', 'mhz', 'watts',
                'fan', 'rpm', 'voltage', 'energy', 'utilization',
            )
            sensors = [s for s in sensors if s.description and any(k in s.description.lower() for k in sensor_keywords)]

            from app.workload_profile import (
                build_workload_profile,
                option_profile_key,
                sensor_is_relevant,
            )
            from app.sensor_quality import chart_has_usable_signal, series_quality

            config_args_for_wl = args_val if args_val is not None else ""
            workload_profiles_by_option: dict[str, dict] = {}
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
                wl = build_workload_profile(
                    primary_benchmark.title,
                    primary_benchmark.app_version or "",
                    config_args_for_wl,
                    system_ids=sys_id_ints,
                    description=desc or primary_benchmark.description or "",
                    option_description=desc,
                    option_scale=scale,
                )
                workload_profiles_by_option[ok] = wl
                ch["option_key"] = ok
                ch["workload_profile"] = wl

            workload_profile = (
                next(iter(workload_profiles_by_option.values()))
                if len(workload_profiles_by_option) == 1
                else None
            )
            filter_sensors = (request.args.get('filter_sensors') or '1').lower() not in {'0', 'false', 'no'}
            filter_noisy = (request.args.get('filter_noisy_sensors') or '1').lower() not in {'0', 'false', 'no'}
            if filter_sensors and workload_profiles_by_option:
                sensors = [
                    s for s in sensors
                    if any(
                        sensor_is_relevant(s.description, s.scale, wp, strict=True)
                        for wp in workload_profiles_by_option.values()
                    )
                ]

            for s_bm in sensors:
                s_traces = []
                for sys_id in sys_args_map:
                    target_args = sys_args_map[sys_id]
                    system = db.session.get(System, sys_id)

                    # Fetch results for this system and this sensor only (s_bm.id), not all sensors.
                    all_s_res = BenchmarkResult.query.filter(
                        BenchmarkResult.system_id == sys_id,
                        BenchmarkResult.benchmark_id == s_bm.id,
                    ).all()
                    # Match sensor results to this primary run by arguments so we don't
                    # attach another run's sensor data (e.g. wrong temps).
                    if not target_args:
                        # For runs whose primary arguments are empty (e.g. "Unix Makefiles"
                        # vs "Ninja"), prefer sensor runs that do NOT contain any of the
                        # non-empty primary argument strings. This lets us pair the
                        # "no-args" run with sensors like "CPU Temp " instead of
                        # "CPU Temp Ninja".
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
                    s_res = matching_s_res[0]
                    system_label = format_system_profile_label(system)
                    short_name = system.identifier

                    trace = {
                        "name": short_name,
                        "type": "bar" if s_bm.display_format == "BAR_GRAPH" else "scatter",
                        "customdata": [system_label],
                        "hovertemplate": "%{customdata[0]}<br>%{x}<extra></extra>" if s_bm.display_format == "BAR_GRAPH" else None
                    }
                    if s_bm.display_format == "BAR_GRAPH":
                        trace["x"] = [short_name]
                        trace["y"] = [s_res.value]
                    elif s_bm.display_format == "LINE_GRAPH":
                        y_data = s_res.data_json or []
                        trace["x"] = list(range(len(y_data)))
                        trace["y"] = y_data
                        trace["mode"] = "lines"

                        if y_data:
                            clean_y = [val for val in y_data if isinstance(val, (int, float))]
                            if clean_y:
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
                                q = series_quality(clean_y, s_bm.description, s_bm.scale)
                                stats_dict["quality"] = q
                                trace["quality"] = q
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

            if charts:
                charts.sort(key=lambda x: not x["is_primary"])
                title = f"{primary_benchmark.title} ({primary_benchmark.app_version})"
                if args_val and (isinstance(args_val, str) and args_val.strip()):
                    title += f" — {args_val}"
                # Compute display label for this run so sensor data is explicitly correlated
                # (e.g. "Unix Makefiles" for empty-args run when other option is "Ninja").
                args_label = None
                if pooling_active:
                    args_label = args_val
                elif args_val and (isinstance(args_val, str) and args_val.strip()):
                    args_label = args_val
                else:
                    # Get the benchmark that corresponds to this args_val (for description).
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
                    sys_obj = db.session.get(System, sid)
                    if sys_obj:
                        system_names.append(sys_obj.identifier)
                pts_scoring = build_pts_context_for_compare_group(
                    title=primary_benchmark.title,
                    app_version=primary_benchmark.app_version or "",
                    identifier=primary_benchmark.identifier,
                    primary_charts=[c for c in charts if c.get("is_primary")],
                    system_ids=system_names,
                    config_args=args_val if args_val is not None else "",
                    ob_index=ob_index_cache,
                )
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
    pts_global_ob = (
        build_pts_ob_global_summary(pts_contexts, first_names)
        if pts_contexts and first_names else None
    )

    return {
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
    }


@app.route('/api/pool_flag_suggestions')
def api_pool_flag_suggestions():
    """
    Suggest argument flags worth pooling for the selected systems/benchmark configs.

    Heuristic: a flag is "worth pooling" when it has >1 distinct values across the
    selected data and at least one value is not shared by all selected systems.
    """
    from app.args_pooling import parse_args_tokens

    system_ids = request.args.getlist('system_ids')
    config_params = request.args.getlist('config')
    if not system_ids:
        return {"error": "Missing system_ids parameter(s)"}, 400

    try:
        sys_id_ints = [int(s) for s in system_ids]
    except (TypeError, ValueError):
        return {"error": "Invalid system_ids"}, 400

    # Parse requested configs (same shape as /api/compare).
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

    # Build per-suite selected args universe.
    suite_to_selected_args: dict[tuple[str, str], set[str | None]] = defaultdict(set)
    for b_id, args_val in config_list:
        bm = db.session.get(Benchmark, b_id)
        if not bm:
            continue
        if not getattr(bm, "is_primary", False):
            cand = Benchmark.query.filter(
                Benchmark.title == bm.title,
                Benchmark.app_version == bm.app_version,
                Benchmark.display_format == "BAR_GRAPH",
                Benchmark.is_primary == True,
            ).first()
            if cand:
                bm = cand
        suite_key = (bm.title, bm.app_version or "")
        suite_to_selected_args[suite_key].add(args_val)

    if not suite_to_selected_args:
        return {"candidates": [], "samples": []}, 200

    selected_sys_set = set(sys_id_ints)
    # flag -> value -> systems
    flag_value_systems: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    # representative sample rows
    sample_rows: list[dict] = []

    def parse_flag_pairs(args_text: str) -> list[tuple[str, str]]:
        """
        Extract (flag, value) pairs from CLI-ish args.
        Supports:
          --flag value, --flag=value, -F value, -Fvalue
        """
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
                # short flag style: -F value or -Fvalue
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
        # Resolve all primary benchmark IDs in this suite for selected systems.
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

        # If user selected explicit args for this suite, keep those; if "all configs" was selected
        # (args None), keep all.
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
            # keep some sample strings
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
        # score: prioritize flags with more non-shared values and better system coverage
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

    # Representative sample subset:
    # one concise example per (top-flag, non-shared value), instead of listing all variants.
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

    # Keep list bounded and stable.
    sample_out = sample_out[:18]

    return {"candidates": candidates[:20], "samples": sample_out}, 200


def _longest_common_prefix(strs):
    """Return the longest string that is a prefix of all non-empty strings in strs."""
    strs = [s for s in strs if s]
    if not strs:
        return ""
    s0, s1 = min(strs), max(strs)
    for i, c in enumerate(s0):
        if i >= len(s1) or c != s1[i]:
            return s0[:i]
    return s0


def _longest_common_suffix(strs):
    rev = [s[::-1] for s in strs if s]
    return _longest_common_prefix(rev)[::-1] if rev else ""


def _unique_part_of_description(empty_description, other_descriptions):
    """
    Given the description for the no-arguments config and descriptions for other
    configs, return the part of empty_description that is not common to all
    (e.g. strip common prefix/suffix so "Build System: Unix Makefiles" with
    others "Build System: Ninja" yields "Unix Makefiles").
    """
    if not (empty_description or "").strip():
        return empty_description or ""
    empty_description = (empty_description or "").strip()
    others = [(d or "").strip() for d in (other_descriptions or []) if (d or "").strip()]
    if not others:
        return empty_description
    common_prefix = _longest_common_prefix([empty_description] + others)
    common_suffix = _longest_common_suffix([empty_description] + others)
    out = empty_description
    if common_prefix:
        out = out.removeprefix(common_prefix)
    if common_suffix:
        out = out.removesuffix(common_suffix)
    return out.strip() or empty_description


@app.route('/api/common_benchmarks')
def api_common_benchmarks():
    system_ids = request.args.getlist('system_id')
    if not system_ids:
        return {"error": "Missing system_ids parameter"}, 400
        
    common_bms = None
    
    for sys_id in system_ids:
        # Get all primary benchmark results for this system
        results = BenchmarkResult.query.filter_by(system_id=sys_id).all()
        # Find which of these results point to primary benchmarks
        res_bm_ids = [r.benchmark_id for r in results]
        primary_bms = Benchmark.query.filter(Benchmark.id.in_(res_bm_ids), Benchmark.is_primary == True).all()
        
        # Group benchmarks logically by (title, version); identifier can differ across systems
        # (e.g. pts/blender-1.2 vs pts/blender-1.2.0), so we treat same title+version as one suite.
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

    # Form response based on the common abstract suite key.
    # Include distinct arguments (configurations) per benchmark for the selected systems.
    try:
        sys_id_ints = [int(s) for s in system_ids]
    except (ValueError, TypeError):
        sys_id_ints = system_ids

    # Group all benchmark IDs by logical key (same benchmark can have different IDs per system)
    key_to_bm_ids = {}
    key_to_one_bm_id = {}
    for key, bm_id in common_bms:
        key_to_bm_ids.setdefault(key, set()).add(bm_id)
        if key not in key_to_one_bm_id:
            key_to_one_bm_id[key] = bm_id

    unique_common_suites = {}
    for key, bm_ids in key_to_bm_ids.items():
        # key is (title, app_version)
        config_rows = db.session.query(
            BenchmarkResult.arguments,
            BenchmarkResult.benchmark_id
        ).filter(
            BenchmarkResult.benchmark_id.in_(bm_ids),
            BenchmarkResult.system_id.in_(sys_id_ints)
        ).distinct().all()
        # One benchmark_id per arguments value to resolve description
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
        # Keep all distinct arguments (including empty) and assign a display label.
        # For empty-args configs, use the part of the description that is unique
        # vs other configs' descriptions (e.g. "Unix Makefiles" from "Build System: Unix Makefiles").
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


@app.route('/api/scatter_candidates')
def api_scatter_candidates():
    """
    Returns top-ranked scatter plot candidates for Performance Insights.

    Y axis (outcome): primary BAR_GRAPH score (Benchmark.is_primary == True)
    X axis (feature): any key from INSIGHT_COMPONENT_KEYS (single feature for v1)

    Backend computes a pragmatic effect-size score and returns only candidates that
    look plausibly correlated, so the frontend can stay uncluttered.
    """
    from app.analyzer import INSIGHT_COMPONENT_KEYS

    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()
    top_k = int(request.args.get('top_k') or 10)
    # Keep this low: users often start with small cohorts and we still want "useful" candidates.
    min_points = int(request.args.get('min_points') or 3)
    min_distinct_x = int(request.args.get('min_distinct_x') or 2)
    min_effect = float(request.args.get('min_effect') or 0.1)

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400

    # 1) Resolve primary benchmark ids for this title/app_version
    bms_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == 'BAR_GRAPH',
        Benchmark.is_primary.is_(True),
    )
    if app_version:
        bms_q = bms_q.filter(Benchmark.app_version == app_version)
    primary_bms = bms_q.all()
    if not primary_bms:
        return {"error": "No primary BAR_GRAPH benchmark found for the given title/app_version"}, 404
    primary_bm_ids = [b.id for b in primary_bms]

    # Determine direction: normalize to "higher is better" for plotting/scoring.
    # Frontend treats:
    # - proportion == 'HIB' as higher is better
    # - proportion == 'LIB' as lower is better
    # - and also supports the strings 'Higher is Better'/'Lower is better'
    def proportion_is_lower_better(p):
        p = (p or '').strip().upper()
        if p == 'LIB':
            return True
        if p == 'HIB':
            return False
        # fall back to textual matching
        pl = (p or '').lower()
        return 'lower' in pl and 'better' in pl

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    # 2) Gather BAR_GRAPH results for those benchmarks and the requested args.
    # Multiple benchmark variants may exist; aggregate per system (mean).
    results = (
        BenchmarkResult.query
        .filter(
            BenchmarkResult.benchmark_id.in_(primary_bm_ids),
            BenchmarkResult.arguments == args_str,
            BenchmarkResult.value.isnot(None),
        )
        .all()
    )
    if not results:
        return {"candidates": [], "meta": {"points": 0}}, 200

    by_system = defaultdict(list)
    for r in results:
        by_system[r.system_id].append(r.value)

    # Load systems and compute their component values once.
    sys_ids = sorted(by_system.keys())
    systems = System.query.filter(System.id.in_(sys_ids)).all()
    comps = {s.id: get_system_components(s) for s in systems}

    # Points for each system
    y_raw_by_system = {}
    y_by_system = {}
    for sid, vals in by_system.items():
        # mean across benchmark variants for this system/args
        y_raw = statistics.mean(vals)
        y_raw_by_system[sid] = y_raw
        y_by_system[sid] = y_raw * y_flip

    def robust_spread(vals):
        # Median absolute deviation-like spread
        if not vals:
            return 0.0
        m = statistics.median(vals)
        abs_dev = [abs(v - m) for v in vals]
        return statistics.median(abs_dev) or 0.0

    def spearman_rho(x, y):
        # Spearman rho via Pearson correlation of ranks (with average rank for ties)
        # x,y are same length and already numeric.
        n = len(x)
        if n < 3:
            return None
        def rank(arr):
            pairs = sorted((v, i) for i, v in enumerate(arr))
            ranks = [0.0] * n
            k = 0
            while k < n:
                v = pairs[k][0]
                j = k
                while j < n and pairs[j][0] == v:
                    j += 1
                # average rank for ties; ranks are 1..n
                avg = (k + 1 + j) / 2.0
                for t in range(k, j):
                    ranks[pairs[t][1]] = avg
                k = j
            return ranks
        rx = rank(x)
        ry = rank(y)
        mx = statistics.mean(rx)
        my = statistics.mean(ry)
        num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        denx = sum((a - mx) ** 2 for a in rx) ** 0.5
        deny = sum((b - my) ** 2 for b in ry) ** 0.5
        if denx == 0 or deny == 0:
            return None
        return num / (denx * deny)

    def parse_version_numeric(s):
        # Extract the first dotted numeric sequence and map it into a comparable float.
        # Examples:
        #  - "6.18.7-760..." -> 6 + 18/1000 + 7/1e6
        #  - "560.35.03" -> 560 + 35/1000 + 3/1e6
        if not s:
            return None
        s = str(s)
        nums = re.findall(r'\d+', s)
        if not nums:
            return None
        n0 = int(nums[0])
        n1 = int(nums[1]) if len(nums) > 1 else 0
        n2 = int(nums[2]) if len(nums) > 2 else 0
        return float(n0) + (n1 / 1000.0) + (n2 / 1_000_000.0)

    def score_numeric(points):
        # points: list of (x_numeric, y)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        spread = robust_spread(ys) or 1e-9
        # Quantile binning
        uniq_x = sorted(set(xs))
        k = min(5, max(2, len(uniq_x)))
        if k < 2:
            return None
        xs_sorted = sorted(points, key=lambda t: t[0])
        # split into k bins by index
        bin_means = []
        for b in range(k):
            lo = int(b * len(points) / k)
            hi = int((b + 1) * len(points) / k)
            if hi <= lo:
                continue
            bin_vals = xs_sorted[lo:hi]
            if not bin_vals:
                continue
            bin_means.append(statistics.mean([t[1] for t in bin_vals]))
        if len(bin_means) < 2:
            return None
        top = max(bin_means)
        bottom = min(bin_means)
        effect = (top - bottom) / spread
        rho = spearman_rho(xs, ys)
        return {"effect": effect, "rho": rho}

    def score_categorical(points):
        # points: list of (x_raw, y)
        by_x = defaultdict(list)
        for x_raw, y in points:
            by_x[x_raw].append(y)
        if len(by_x) < 2:
            return None
        ys_all = [y for _, y in points]
        spread = robust_spread(ys_all) or 1e-9
        means = [statistics.mean(vs) for vs in by_x.values()]
        top = max(means)
        bottom = min(means)
        effect = (top - bottom) / spread
        # heuristic: if best and worst are close to overall median, effect will be small
        return {"effect": effect}

    VERSION_NUMERIC_X_KEYS = {
        "kernel_version",
        "nvidia_driver",
        "mesa_version",
        "llvm_version",
        "vulkan_driver",
    }

    # 3) Evaluate each single-feature X key
    candidates = []
    label_map = dict(COMPARE_BY_OPTIONS)
    for x_key in INSIGHT_COMPONENT_KEYS:
        # gather system points for this feature: (system_id, x_raw, y)
        raw_points = []
        for sid in sys_ids:
            x_raw = (comps.get(sid, {}).get(x_key) or '').strip()
            if not x_raw:
                continue
            y = y_by_system.get(sid)
            if y is None:
                continue
            raw_points.append((sid, x_raw, y))

        if len(raw_points) < min_points:
            continue
        distinct_x = len({p[1] for p in raw_points})
        if distinct_x < min_distinct_x:
            continue

        # decide numeric vs categorical
        numeric_points = []
        numeric_parsed = 0
        categorical_points = []
        for sid, x_raw, y in raw_points:
            x_num = parse_version_numeric(x_raw)
            if x_num is not None:
                numeric_parsed += 1
                numeric_points.append((x_num, y))
            categorical_points.append((x_raw, y))

        numeric_mode = (x_key in VERSION_NUMERIC_X_KEYS) and (numeric_parsed >= 3) and (numeric_parsed / max(1, len(raw_points)) >= 0.8)

        if numeric_mode:
            scored = score_numeric(numeric_points)
            if not scored:
                continue
            effect = scored.get("effect")
            if effect is None or effect < min_effect:
                continue

            # Return points in a Plotly-friendly format.
            points_out = []
            for sid, x_raw, y in raw_points:
                points_out.append({
                    "system_id": sid,
                    "x": x_raw,
                    "x_numeric": parse_version_numeric(x_raw),
                    "y": y_raw_by_system.get(sid),
                    "y_raw": y_raw_by_system.get(sid),
                    "y_normalized": y,
                })

            candidates.append({
                "x_key": x_key,
                "x_label": label_map.get(x_key, x_key),
                "type": "numeric",
                "effect_score": effect,
                "spearman_rho": scored.get("rho"),
                "point_count": len(raw_points),
                "distinct_x": distinct_x,
                "points": points_out,
            })
        else:
            scored = score_categorical(categorical_points)
            if not scored:
                continue
            effect = scored.get("effect")
            if effect is None or effect < min_effect:
                continue

            points_out = []
            for sid, x_raw, y in raw_points:
                points_out.append({
                    "system_id": sid,
                    "x": x_raw,
                    "y": y_raw_by_system.get(sid),
                    "y_raw": y_raw_by_system.get(sid),
                    "y_normalized": y,
                })

            candidates.append({
                "x_key": x_key,
                "x_label": label_map.get(x_key, x_key),
                "type": "categorical",
                "effect_score": effect,
                "point_count": len(raw_points),
                "distinct_x": distinct_x,
                "points": points_out,
            })

    candidates.sort(key=lambda c: (c["effect_score"], c.get("spearman_rho") or 0), reverse=True)
    y_label_base = primary_bms[0].scale or "Score"
    lower_better = is_lower_better
    y_label = f"{y_label_base} ({'lower is better' if lower_better else 'higher is better'})"
    return {
        "candidates": candidates[:top_k],
        "meta": {
            "benchmark_title": title,
            "app_version": app_version,
            "args": args_str,
            "primary_benchmark_count": len(primary_bm_ids),
            "systems_with_primary_y": len(y_by_system),
            "min_points": min_points,
            "min_distinct_x": min_distinct_x,
            "min_effect": min_effect,
                "y_axis_label": y_label,
                "y_flip": y_flip,
        }
    }, 200


@app.route('/api/variance_feature_map')
def api_variance_feature_map():
    """
    Returns a "within-system variability vs between-cohort dominance" map across feature keys.

    For each feature_key (and optional pairs), each point summarizes:
      X = average within-system run variability
          (computed as stdev across BAR_GRAPH per-run values, per system, then averaged)
      Y = dominance magnitude (best cohort mean - worst cohort mean), in raw units

    This directly separates:
      - variability within a system (run-to-run noise)
      - variability between systems/cohorts (component-driven performance shifts)
    """
    from app.analyzer import INSIGHT_COMPONENT_KEYS, MIN_SYSTEMS_TOTAL, MIN_SYSTEMS_PER_COHORT

    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()

    top_k = int(request.args.get('top_k') or 10)
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    include_pairs = (request.args.get('include_pairs') or '1').lower() not in {'0', 'false', 'no'}
    min_feature_delta = float(request.args.get('min_feature_delta') or 0.0)

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400

    bms_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == 'BAR_GRAPH',
        Benchmark.is_primary.is_(True),
    )
    if app_version:
        bms_q = bms_q.filter(Benchmark.app_version == app_version)
    primary_bms = bms_q.all()
    if not primary_bms:
        return {"error": "No primary BAR_GRAPH benchmark found for the given title/app_version"}, 404

    rep_bm = primary_bms[0]
    label_map = dict(COMPARE_BY_OPTIONS)
    y_label_base = rep_bm.scale or "Score"

    def proportion_is_lower_better(p):
        p = (p or '').strip().upper()
        if p == 'LIB':
            return True
        if p == 'HIB':
            return False
        pl = (p or '').lower()
        return 'lower' in pl and 'better' in pl

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    # Analyzer buckets empty args as 'default'
    args_analysis_key = 'default' if (not args_str or args_str.lower() == 'default') else args_str
    args_db = '' if args_analysis_key == 'default' else args_str

    all_results = BenchmarkResult.query.filter(
        BenchmarkResult.benchmark_id.in_([b.id for b in primary_bms]),
        BenchmarkResult.arguments == args_db,
        BenchmarkResult.value.isnot(None),
    ).all()

    if not all_results:
        return {"points": [], "meta": {"y_label": y_label_base, "x_label": "within-system run variability (stdev)"}}, 200

    # Build per-system run arrays (BAR_GRAPH per-run values), then derive:
    #   - system mean raw
    #   - system mean normalized (higher=better)
    #   - within-system run variability (stddev of runs)
    by_system_run_vals = defaultdict(list)
    for r in all_results:
        run_vals = []
        if isinstance(r.data_json, list):
            for v in r.data_json:
                if v is None:
                    continue
                try:
                    run_vals.append(float(v))
                except (ValueError, TypeError):
                    pass
        if not run_vals and r.value is not None:
            run_vals = [float(r.value)]
        if run_vals:
            by_system_run_vals[r.system_id].extend(run_vals)

    if not by_system_run_vals:
        return {"points": [], "meta": {"y_label": y_label_base, "x_label": "within-system run variability (stdev)"}}, 200

    y_raw_mean_by_system = {}
    y_norm_mean_by_system = {}
    within_system_std_by_system = {}
    for sid, run_vals in by_system_run_vals.items():
        y_raw_mean = statistics.mean(run_vals)
        y_raw_mean_by_system[sid] = y_raw_mean
        y_norm_mean_by_system[sid] = y_raw_mean * y_flip
        within_system_std_by_system[sid] = statistics.stdev(run_vals) if len(run_vals) >= 2 else 0.0

    sys_ids = sorted(y_raw_mean_by_system.keys())
    systems = System.query.filter(System.id.in_(sys_ids)).all()
    comps_by_sid = {s.id: get_system_components(s) for s in systems}

    points = []

    def add_feature_point(feature_type, feature_key, system_groups):
        """
        system_groups: dict of cohort_value -> list of system summaries
                        summaries are (system_mean_raw, system_mean_norm, system_run_stddev)
        """
        if not system_groups:
            return

        total_systems_with_feature = sum(len(v) for v in system_groups.values())
        if total_systems_with_feature < MIN_SYSTEMS_TOTAL:
            return

        # Filter cohort values by evidence (minimum number of systems sharing the SAME value)
        valid_groups = []
        for cohort_val, sys_summaries in system_groups.items():
            if len(sys_summaries) < min_cohort_n:
                continue
            valid_groups.append((cohort_val, sys_summaries))

        if len(valid_groups) < 2:
            return

        # Cohort means (computed across systems in that cohort)
        group_rows = []
        for cohort_val, sys_summaries in valid_groups:
            mean_raw = statistics.mean([t[0] for t in sys_summaries])
            mean_norm = statistics.mean([t[1] for t in sys_summaries])
            avg_within_std = statistics.mean([t[2] for t in sys_summaries]) if sys_summaries else 0.0
            group_rows.append((cohort_val, mean_raw, mean_norm, avg_within_std))

        # Higher=better after normalization, so best cohort is max mean_norm.
        best_row = max(group_rows, key=lambda r: r[2])
        worst_row = min(group_rows, key=lambda r: r[2])

        _, best_mean_raw, _, _ = best_row
        _, worst_mean_raw, _, _ = worst_row

        dominance_delta_raw = abs(best_mean_raw - worst_mean_raw)
        if dominance_delta_raw < min_feature_delta:
            return

        within_system_var_avg = statistics.mean([r[3] for r in group_rows]) if group_rows else 0.0

        # Higher dominance is good; higher within-system noise penalizes usefulness.
        dominance_score = dominance_delta_raw / (within_system_var_avg + 1e-9)

        points.append({
            "feature_type": feature_type,  # 'single' or 'pair'
            "feature_key": feature_key,
            "feature_label": label_map.get(feature_key, feature_key),
            # x is the "within-system variability" axis
            "x_within_spread": within_system_var_avg,
            # y is the "between-cohort dominance" axis
            "y_dominance_delta_raw": dominance_delta_raw,
            "dominance_score": dominance_score,
            "distinct_cohort_values": len(group_rows),
            "systems_with_feature": total_systems_with_feature,
            "best_mean_raw": best_mean_raw,
            "worst_mean_raw": worst_mean_raw,
        })

    # Single features
    for feature_key in INSIGHT_COMPONENT_KEYS:
        system_groups = defaultdict(list)  # cohort_value -> [(mean_raw, mean_norm, run_stddev), ...]
        systems_with_feature = set()
        for sid, mean_raw in y_raw_mean_by_system.items():
            v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
            if not v:
                continue
            systems_with_feature.add(sid)
            system_groups[v].append((
                mean_raw,
                y_norm_mean_by_system.get(sid, mean_raw * y_flip),
                within_system_std_by_system.get(sid, 0.0),
            ))

        if len(systems_with_feature) < MIN_SYSTEMS_TOTAL:
            continue

        add_feature_point("single", feature_key, system_groups)

    # Pair features (optional)
    if include_pairs:
        pair_defs = [
            ("processor", "memory"),
            ("processor", "cooler_model"),
            ("processor", "graphics"),
            ("graphics", "memory"),
        ]
        for k1, k2 in pair_defs:
            pair_groups = defaultdict(list)  # (v1,v2) -> [(mean_raw, mean_norm, run_stddev)]
            systems_with_pair = set()
            for sid, mean_raw in y_raw_mean_by_system.items():
                c1 = (comps_by_sid.get(sid, {}).get(k1) or '').strip()
                c2 = (comps_by_sid.get(sid, {}).get(k2) or '').strip()
                if not c1 or not c2:
                    continue
                systems_with_pair.add(sid)
                pair_groups[(c1, c2)].append((
                    mean_raw,
                    y_norm_mean_by_system.get(sid, mean_raw * y_flip),
                    within_system_std_by_system.get(sid, 0.0),
                ))

            if len(systems_with_pair) < MIN_SYSTEMS_TOTAL:
                continue

            feature_key = f"{k1}+{k2}"
            feature_label = f"{label_map.get(k1,k1)} + {label_map.get(k2,k2)}"
            # Reuse add_feature_point but with a temporary label map entry.
            # We won't store the intermediate cohort values, only aggregate stats.
            add_feature_point("pair", feature_key, pair_groups)
            if points:
                points[-1]["feature_label"] = feature_label

    # Sort and trim
    points.sort(key=lambda p: p["dominance_score"], reverse=True)
    points = points[:top_k]

    return {
        "points": points,
        "meta": {
            "benchmark_title": title,
            "app_version": app_version,
            "args": args_analysis_key,
            "y_label": y_label_base,
            "x_label": "within-system run variability (stddev of runs)",
            "direction": "y_dominance_delta_raw is best-worst mean diff across cohorts (always positive); x is within-system run noise",
            "y_flip": y_flip,
            "min_cohort_n": min_cohort_n,
            "min_feature_delta": min_feature_delta,
        }
    }, 200


@app.route('/api/variance_leaderboard')
def api_variance_leaderboard():
    """
    Compute a leaderboard of component features that explain benchmark differences best.

    We rank primarily by one-way eta-squared: fraction of total score variance that lies
    *between* component cohorts (larger = more separation across component values).

    Rows require at least one cohort with 2+ systems so pure per-system labels
    (100% eta² with no replication) are omitted.
    """
    from app.analyzer import INSIGHT_COMPONENT_KEYS, MIN_SYSTEMS_TOTAL

    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()

    top_k = int(request.args.get('top_k') or 10)
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_distinct_cohorts = int(request.args.get('min_distinct_cohorts') or 2)
    include_pairs = (request.args.get('include_pairs') or '1').lower() not in {'0', 'false', 'no'}

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400

    bms_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == 'BAR_GRAPH',
        Benchmark.is_primary.is_(True),
    )
    if app_version:
        bms_q = bms_q.filter(Benchmark.app_version == app_version)
    primary_bms = bms_q.all()
    if not primary_bms:
        return {"error": "No primary BAR_GRAPH benchmark found for the given title/app_version"}, 404

    rep_bm = primary_bms[0]
    label_map = dict(COMPARE_BY_OPTIONS)
    y_label_base = rep_bm.scale or "Score"

    def proportion_is_lower_better(p):
        p = (p or '').strip().upper()
        if p == 'LIB':
            return True
        if p == 'HIB':
            return False
        pl = (p or '').lower()
        return 'lower' in pl and 'better' in pl

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    # Analyzer buckets empty args as 'default'; DB stores empty args as ''.
    args_analysis_key = 'default' if (not args_str or args_str.lower() == 'default') else args_str
    args_db = '' if args_analysis_key == 'default' else args_str

    primary_bm_ids = [b.id for b in primary_bms]
    all_results = BenchmarkResult.query.filter(
        BenchmarkResult.benchmark_id.in_(primary_bm_ids),
        BenchmarkResult.arguments == args_db,
        BenchmarkResult.value.isnot(None),
    ).all()

    if not all_results:
        return {
            "rows": [],
            "meta": {
                "benchmark_title": title,
                "app_version": app_version,
                "args": args_analysis_key,
                "y_label": y_label_base,
                "x_label": "within-bucket spread",
                "min_cohort_n": min_cohort_n,
                "include_pairs": include_pairs,
            }
        }, 200

    # Per system mean of runs/entries for BAR_GRAPH.
    by_system_vals = defaultdict(list)
    for r in all_results:
        by_system_vals[r.system_id].append(r.value)

    y_norm_by_system = {}
    y_raw_by_system = {}
    for sid, vals in by_system_vals.items():
        y_raw = statistics.mean([v for v in vals if v is not None])
        y_raw_by_system[sid] = y_raw
        y_norm_by_system[sid] = y_raw * y_flip

    sys_ids = sorted(y_raw_by_system.keys())
    systems = System.query.filter(System.id.in_(sys_ids)).all()
    comps_by_sid = {s.id: get_system_components(s) for s in systems}

    all_y_norm = [y_norm_by_system[sid] for sid in sys_ids]

    def robust_spread(vals):
        # Median absolute deviation-like; non-negative; stable on small samples.
        if not vals:
            return 0.0
        m = statistics.median(vals)
        abs_dev = [abs(v - m) for v in vals]
        return statistics.median(abs_dev) or 0.0

    overall_spread = robust_spread(all_y_norm)
    overall_spread_eps = overall_spread + 1e-9

    text_blob = " ".join([
        (rep_bm.title or ""),
        (rep_bm.description or ""),
        args_str or "",
    ]).lower()
    wl_ctx = _insights_workload_context_from_analysis(title, app_version, args_analysis_key, text_blob)
    scope = wl_ctx["scope"]
    active_bottlenecks = list(wl_ctx.get("active_bottlenecks") or [])
    if scope == "general":
        scope = _insights_infer_scope(text_blob)
        active_bottlenecks = [scope] if scope in {"cpu", "gpu", "storage", "memory"} else []
    scope_override = (request.args.get('scope') or '').strip().lower()
    include_all_component_keys = scope_override == "all"
    if scope_override in {"all", "general"}:
        scope = "general"
        active_bottlenecks = []
    elif scope_override in {"cpu", "gpu", "storage", "memory"}:
        scope = scope_override
        active_bottlenecks = [scope_override]
    allowed_singles = _insights_allowed_singles_for_scope(
        scope, include_all_component_keys, active_bottlenecks or None,
    )

    rows = []

    def eval_single(feature_key):
        buckets = defaultdict(list)
        for sid in sys_ids:
            v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
            if not v:
                continue
            buckets[v].append(y_norm_by_system[sid])

        systems_with_nonempty = sum(len(ys) for ys in buckets.values())
        if systems_with_nonempty < MIN_SYSTEMS_TOTAL:
            return
        if len(buckets) < min_distinct_cohorts:
            return
        # Drop identifier-like splits: every cohort must not be all singletons.
        if not any(len(ys) >= 2 for ys in buckets.values()):
            return

        vals = []
        for ys in buckets.values():
            vals.extend(ys)
        grand_mean = statistics.mean(vals)
        ss_total = sum((y - grand_mean) ** 2 for y in vals)
        if ss_total < 1e-18:
            return

        ss_between = 0.0
        for ys in buckets.values():
            nj = len(ys)
            mj = statistics.mean(ys)
            ss_between += nj * (mj - grand_mean) ** 2
        eta_sq = ss_between / ss_total

        bucket_spreads = []
        for ys in buckets.values():
            s = robust_spread(ys)
            bucket_spreads.append((s, len(ys)))
        total_w = sum(n for _, n in bucket_spreads) or 1
        conditional_spread = sum(s * n for s, n in bucket_spreads) / total_w
        reduction_ratio = 1.0 - (conditional_spread / overall_spread_eps)

        cohorts_meeting_min_n = sum(1 for ys in buckets.values() if len(ys) >= min_cohort_n)

        rows.append({
            "feature_type": "single",
            "feature_key": feature_key,
            "feature_label": label_map.get(feature_key, feature_key),
            "eta_squared": eta_sq,
            "reduction_ratio": reduction_ratio,
            "overall_spread": overall_spread,
            "conditional_spread": conditional_spread,
            "distinct_cohort_values": len(buckets),
            "systems_with_feature": systems_with_nonempty,
            "cohorts_meeting_min_n": cohorts_meeting_min_n,
            "min_cohort_n": min_cohort_n,
        })

    def eval_pair(k1, k2):
        buckets = defaultdict(list)
        for sid in sys_ids:
            c1 = (comps_by_sid.get(sid, {}).get(k1) or '').strip()
            c2 = (comps_by_sid.get(sid, {}).get(k2) or '').strip()
            if not c1 or not c2:
                continue
            buckets[(c1, c2)].append(y_norm_by_system[sid])

        systems_with_pair = sum(len(ys) for ys in buckets.values())
        if systems_with_pair < MIN_SYSTEMS_TOTAL:
            return
        if len(buckets) < min_distinct_cohorts:
            return
        if not any(len(ys) >= 2 for ys in buckets.values()):
            return

        vals = []
        for ys in buckets.values():
            vals.extend(ys)
        grand_mean = statistics.mean(vals)
        ss_total = sum((y - grand_mean) ** 2 for y in vals)
        if ss_total < 1e-18:
            return

        ss_between = 0.0
        for ys in buckets.values():
            nj = len(ys)
            mj = statistics.mean(ys)
            ss_between += nj * (mj - grand_mean) ** 2
        eta_sq = ss_between / ss_total

        bucket_spreads = []
        for ys in buckets.values():
            s = robust_spread(ys)
            bucket_spreads.append((s, len(ys)))
        total_w = sum(n for _, n in bucket_spreads) or 1
        conditional_spread = sum(s * n for s, n in bucket_spreads) / total_w
        reduction_ratio = 1.0 - (conditional_spread / overall_spread_eps)
        cohorts_meeting_min_n = sum(1 for ys in buckets.values() if len(ys) >= min_cohort_n)

        rows.append({
            "feature_type": "pair",
            "feature_key": f"{k1}+{k2}",
            "feature_label": f"{label_map.get(k1,k1)} + {label_map.get(k2,k2)}",
            "eta_squared": eta_sq,
            "reduction_ratio": reduction_ratio,
            "overall_spread": overall_spread,
            "conditional_spread": conditional_spread,
            "distinct_cohort_values": len(buckets),
            "systems_with_feature": systems_with_pair,
            "cohorts_meeting_min_n": cohorts_meeting_min_n,
            "min_cohort_n": min_cohort_n,
        })

    # Singles
    for feature_key in allowed_singles:
        eval_single(feature_key)

    # Pairs
    if include_pairs:
        # Only evaluate pairs that match the inferred scope.
        if scope == "cpu":
            pair_defs = [("processor", "memory"), ("processor", "cooler_model")]
        elif scope == "gpu":
            pair_defs = [("processor", "graphics"), ("graphics", "memory")]
        elif scope == "storage":
            pair_defs = [("processor", "memory")]
        else:
            pair_defs = [("processor", "memory"), ("processor", "cooler_model"), ("processor", "graphics"), ("graphics", "memory")]

        for k1, k2 in pair_defs:
            eval_pair(k1, k2)

    rows.sort(
        key=lambda r: (r["eta_squared"], r["cohorts_meeting_min_n"], r["reduction_ratio"]),
        reverse=True,
    )
    rows = rows[:top_k]

    return {
        "rows": rows,
        "meta": {
            "benchmark_title": title,
            "app_version": app_version,
            "args": args_analysis_key,
            "y_label": y_label_base,
            "x_label": "between-cohort share of variance (eta²)",
            "overall_spread": overall_spread,
            "overall_spread_eps": overall_spread_eps,
            "min_cohort_n": min_cohort_n,
            "min_distinct_cohorts": min_distinct_cohorts,
            "include_pairs": include_pairs,
            "feature_scope": scope,
            "ranking": "eta_squared_primary",
            "require_replicated_cohort": True,
        }
    }, 200


@app.route('/api/insights_eligible_groupby')
def api_insights_eligible_groupby():
    """
    Components that have enough cohort replication to compare spread safely
    (same gates as the variance leaderboard for singles): min_distinct cohorts,
    at least one cohort with min_cohort_n systems.
    """
    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()
    scope_override = (request.args.get('scope') or '').strip().lower()
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_distinct_cohorts = int(request.args.get('min_distinct_cohorts') or 2)

    bundle, err = _load_primary_insights_bundle(title, app_version, args_str, scope_override)
    if err:
        return {"error": err[0]}, err[1]

    label_map = bundle["label_map"]
    y_norm_by_system = bundle["y_norm_by_system"]
    y_raw_by_system = bundle["y_raw_by_system"]
    sys_ids = bundle["sys_ids"]
    comps_by_sid = bundle["comps_by_sid"]
    min_systems_total = bundle["MIN_SYSTEMS_TOTAL"]

    features = []
    for feature_key in bundle["allowed_singles"]:
        buckets_sid = defaultdict(list)
        for sid in sys_ids:
            v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
            if not v:
                continue
            buckets_sid[v].append(sid)

        buckets = defaultdict(list)
        for v, sids in buckets_sid.items():
            for sid in sids:
                buckets[v].append(y_norm_by_system[sid])

        systems_with_nonempty = sum(len(ys) for ys in buckets.values())
        if systems_with_nonempty < min_systems_total:
            continue
        if len(buckets) < min_distinct_cohorts:
            continue
        if not any(len(ys) >= min_cohort_n for ys in buckets.values()):
            continue

        vals = []
        for ys in buckets.values():
            vals.extend(ys)
        grand_mean = statistics.mean(vals)
        ss_total = sum((y - grand_mean) ** 2 for y in vals)
        if ss_total < 1e-18:
            continue
        ss_between = 0.0
        for ys in buckets.values():
            nj = len(ys)
            mj = statistics.mean(ys)
            ss_between += nj * (mj - grand_mean) ** 2
        eta_sq = ss_between / ss_total

        sn_ratio, spread_raw = _insights_signal_to_noise_raw(buckets_sid, y_raw_by_system)
        tier, tier_summary = _insights_alignment_tier(eta_sq, sn_ratio)
        rank_score = _insights_alignment_rank_score(eta_sq, sn_ratio)

        features.append({
            "feature_key": feature_key,
            "feature_label": label_map.get(feature_key, feature_key),
            "eta_squared": eta_sq,
            "signal_to_noise": sn_ratio,
            "cohort_mean_spread_raw": spread_raw,
            "alignment_tier": tier,
            "alignment_summary": tier_summary,
            "alignment_rank_score": rank_score,
            "n_distinct_values": len(buckets),
            "max_cohort_size": max(len(ys) for ys in buckets.values()),
            "n_systems": systems_with_nonempty,
        })

    features.sort(key=lambda x: (x["alignment_rank_score"], x["eta_squared"]), reverse=True)
    for i, row in enumerate(features, start=1):
        row["alignment_rank"] = i

    return {
        "features": features,
        "meta": {
            "benchmark_title": bundle["title"],
            "app_version": bundle["app_version"],
            "args": bundle["args_analysis_key"],
            "feature_scope": bundle["scope"],
            "min_cohort_n": min_cohort_n,
            "min_distinct_cohorts": min_distinct_cohorts,
            "alignment_ranking_note": (
                "alignment_rank_score blends eta² (between-cohort variance share) and "
                "signal-to-noise (spread of cohort means vs median within-cohort stdev). "
                "Tiers are heuristics for association, not proof a part drives the score."
            ),
        },
    }, 200


@app.route('/api/insights_cohort_spread')
def api_insights_cohort_spread():
    """Per-cohort mean and per-system raw scores for one component (feature_key)."""
    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()
    feature_key = (request.args.get('feature_key') or '').strip()
    scope_override = (request.args.get('scope') or '').strip().lower()
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_distinct_cohorts = int(request.args.get('min_distinct_cohorts') or 2)

    if not feature_key:
        return {"error": "Missing feature_key query parameter"}, 400

    bundle, err = _load_primary_insights_bundle(title, app_version, args_str, scope_override)
    if err:
        return {"error": err[0]}, err[1]

    if feature_key not in bundle["allowed_singles"]:
        return {"error": "feature_key is not allowed for this benchmark scope"}, 400

    y_raw_by_system = bundle["y_raw_by_system"]
    sys_ids = bundle["sys_ids"]
    comps_by_sid = bundle["comps_by_sid"]
    systems_by_id = bundle["systems_by_id"]
    min_systems_total = bundle["MIN_SYSTEMS_TOTAL"]

    buckets = defaultdict(list)
    for sid in sys_ids:
        v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
        if not v:
            continue
        buckets[v].append((sid, y_raw_by_system[sid]))

    systems_with_nonempty = sum(len(pairs) for pairs in buckets.values())
    if systems_with_nonempty < min_systems_total:
        return {"error": "Insufficient systems with this feature populated"}, 400
    if len(buckets) < min_distinct_cohorts:
        return {"error": "Need at least min_distinct_cohorts values for this feature"}, 400
    if not any(len(pairs) >= min_cohort_n for pairs in buckets.values()):
        return {"error": "Need at least one cohort with min_cohort_n systems"}, 400

    is_lower_better = bundle["is_lower_better"]
    cohort_rows = []
    for v, pairs in buckets.items():
        ys_raw = [p[1] for p in pairs]
        mean_raw = statistics.mean(ys_raw)
        stdev_raw = statistics.stdev(ys_raw) if len(ys_raw) > 1 else 0.0
        cohort_rows.append({
            "value": v,
            "n": len(pairs),
            "mean_raw": mean_raw,
            "stdev_raw": stdev_raw,
            "systems": [
                {
                    "system_id": sid,
                    "label": format_system_profile_label(systems_by_id[sid]),
                    "y_raw": yr,
                }
                for sid, yr in pairs
            ],
        })

    cohort_rows.sort(key=lambda c: c["mean_raw"], reverse=not is_lower_better)
    means = [c["mean_raw"] for c in cohort_rows]
    spread = (max(means) - min(means)) if means else 0.0
    inner_stds = [c["stdev_raw"] for c in cohort_rows if c["n"] > 1]
    med_inner = statistics.median(inner_stds) if inner_stds else 0.0
    sn_ratio = float(spread / (med_inner + 1e-9))

    norm_by_val = defaultdict(list)
    y_norm_by_system = bundle["y_norm_by_system"]
    for c in cohort_rows:
        for s in c["systems"]:
            norm_by_val[c["value"]].append(y_norm_by_system[s["system_id"]])
    eta_sel = _insights_eta_squared_norm_buckets(norm_by_val)
    tier, tier_summary = _insights_alignment_tier(eta_sel, sn_ratio)

    pairwise = []
    for i in range(len(cohort_rows)):
        for j in range(i + 1, len(cohort_rows)):
            hi, lo = cohort_rows[i], cohort_rows[j]
            pairwise.append({
                "rank_a": i + 1,
                "rank_b": j + 1,
                "cohort_a_value": hi["value"],
                "cohort_b_value": lo["value"],
                "mean_a_raw": hi["mean_raw"],
                "mean_b_raw": lo["mean_raw"],
                "mean_gap_raw": abs(hi["mean_raw"] - lo["mean_raw"]),
                "note": "Rank 1 is best performance for this benchmark (raw units).",
            })

    from app.hardware_ranks import theoretical_alignment_payload
    theoretical_alignment = theoretical_alignment_payload(feature_key, cohort_rows, is_lower_better)

    return {
        "feature_key": feature_key,
        "feature_label": bundle["label_map"].get(feature_key, feature_key),
        "cohorts": cohort_rows,
        "pairwise_ordered": pairwise,
        "theoretical_alignment": theoretical_alignment,
        "meta": {
            "benchmark_title": bundle["title"],
            "app_version": bundle["app_version"],
            "args": bundle["args_analysis_key"],
            "y_label": bundle["y_label_base"],
            "is_lower_better": is_lower_better,
            "feature_scope": bundle["scope"],
            "cohort_mean_spread_raw": spread,
            "signal_to_noise": sn_ratio,
            "eta_squared": eta_sel,
            "alignment_tier": tier,
            "alignment_summary": tier_summary,
        },
    }, 200


@app.route('/api/variance_leaderboard_coverage')
def api_variance_leaderboard_coverage():
    """
    Returns benchmark+args keys for the Performance Insights dropdown.

    Includes configs from BenchmarkAnalysis when cohort gates pass, and unions configs
    that have enough distinct systems in primary BAR_GRAPH results (including empty
    arguments, keyed as \"default\") so missing profiles do not disappear from the UI.
    """
    from app.analyzer import INSIGHT_COMPONENT_KEYS

    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_distinct_cohorts = int(request.args.get('min_distinct_cohorts') or 2)

    # Keep only analyses whose (title, app_version) has at least one primary BAR_GRAPH benchmark.
    primary_pairs = set(
        (b.title, b.app_version or '')
        for b in Benchmark.query.filter(
            Benchmark.display_format == 'BAR_GRAPH',
            Benchmark.is_primary.is_(True),
        ).all()
    )
    analyses = [
        a for a in BenchmarkAnalysis.query.all()
        if (a.benchmark_title, a.benchmark_app_version or '') in primary_pairs
    ]

    # (benchmark_title, app_version) -> set(args)
    buckets = defaultdict(set)
    from app.analyzer import MIN_SYSTEMS_TOTAL as _INS_MIN_SYSTEMS

    for a in analyses:
        if not a.analysis_json:
            continue
        b_title = a.benchmark_title
        b_app = a.benchmark_app_version or ''

        for args_key, feature_stats in (a.analysis_json or {}).items():
            if not isinstance(feature_stats, dict) or str(args_key).startswith("_"):
                continue

            has_any_feature = False
            for feature_key, feature_values in feature_stats.items():
                # feature_values is expected to be a list of val_stat dicts or [{error:...}]
                if not isinstance(feature_values, list) or not feature_values:
                    continue
                if feature_values[0].get('error'):
                    continue

                cohorts = [
                    v for v in feature_values
                    if isinstance(v, dict) and not v.get('error') and (v.get('n') or 0) >= 1
                ]
                if len(cohorts) < min_distinct_cohorts:
                    continue
                if sum((v.get('n') or 0) for v in cohorts) < _INS_MIN_SYSTEMS:
                    continue
                has_any_feature = True
                break

            if has_any_feature:
                buckets[(b_title, b_app)].add(args_key)

    # Also surface any (title, app_version, arguments) that has enough systems in the DB,
    # so empty-argument profiles are not missing when analysis_json skipped or failed them.
    from sqlalchemy import func as sa_func

    cov_rows = (
        db.session.query(
            Benchmark.title,
            Benchmark.app_version,
            BenchmarkResult.arguments,
            sa_func.count(sa_func.distinct(BenchmarkResult.system_id)).label("n_sys"),
        )
        .join(BenchmarkResult, BenchmarkResult.benchmark_id == Benchmark.id)
        .filter(
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary.is_(True),
            BenchmarkResult.value.isnot(None),
        )
        .group_by(Benchmark.title, Benchmark.app_version, BenchmarkResult.arguments)
        .having(sa_func.count(sa_func.distinct(BenchmarkResult.system_id)) >= _INS_MIN_SYSTEMS)
        .all()
    )
    for t, av, arg, _n in cov_rows:
        pair = (t, av or "")
        if pair not in primary_pairs:
            continue
        cfg_key = "default" if (arg is None or str(arg).strip() == "") else str(arg).strip()
        buckets[pair].add(cfg_key)

    out = []
    for (b_title, b_app), args_set in sorted(buckets.items(), key=lambda t: (t[0][0], t[0][1])):
        out.append({
            "benchmark_title": b_title,
            "app_version": b_app,
            "args": sorted(list(args_set)),
        })

    return {"benchmarks": out, "meta": {"min_cohort_n": min_cohort_n, "min_distinct_cohorts": min_distinct_cohorts}}, 200


@app.route('/api/explain_underperformance')
def api_explain_underperformance():
    """
    Explain why a specific system underperforms on a benchmark/config by ranking
    the system's component values that fall into the worst-performing cohorts.

    This is association-based (not causal): it uses cohort mean performance for each
    component value and compares to the best cohort mean for that feature.
    """
    from app.analyzer import INSIGHT_COMPONENT_KEYS, MIN_SYSTEMS_TOTAL, MIN_SYSTEMS_PER_COHORT

    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()
    system_id_raw = request.args.get('system_id')

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400
    if not system_id_raw:
        return {"error": "Missing system_id query parameter"}, 400
    try:
        system_id = int(system_id_raw)
    except (ValueError, TypeError):
        return {"error": "Invalid system_id"}, 400

    top_n_components = int(request.args.get('top_n_components') or 6)
    top_n_pairs = int(request.args.get('top_n_pairs') or 3)
    include_pairs = (request.args.get('include_pairs') or '1').lower() not in {'0', 'false', 'no'}
    # Evidence thresholds to avoid "everything differs" when cohorts are singletons.
    # - min_cohort_n: minimum number of systems that share the SAME component value
    # - min_pair_n: minimum number of systems that share the SAME component pair
    min_cohort_n = int(request.args.get('min_cohort_n') or 2)
    min_pair_n = int(request.args.get('min_pair_n') or 2)

    label_map = dict(COMPARE_BY_OPTIONS)

    # Resolve primary benchmark ids for this title/app_version.
    bms_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == 'BAR_GRAPH',
        Benchmark.is_primary.is_(True),
    )
    if app_version:
        bms_q = bms_q.filter(Benchmark.app_version == app_version)
    primary_bms = bms_q.all()
    if not primary_bms:
        return {"error": "No primary BAR_GRAPH benchmark found for this title/app_version"}, 404
    primary_bm_ids = [b.id for b in primary_bms]

    # Determine direction: normalize to "higher is better".
    def proportion_is_lower_better(p):
        p = (p or '').strip().upper()
        if p == 'LIB':
            return True
        if p == 'HIB':
            return False
        pl = (p or '').lower()
        return 'lower' in pl and 'better' in pl

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    # Analyzer uses 'default' as the bucket label for empty BenchmarkResult.arguments.
    # The DB stores empty args as ''.
    args_analysis_key = 'default' if (not args_str or args_str.lower() == 'default') else args_str
    args_db = '' if args_analysis_key == 'default' else args_str

    # Gather all primary results for this benchmark/config and compute per-system mean.
    all_results = BenchmarkResult.query.filter(
        BenchmarkResult.benchmark_id.in_(primary_bm_ids),
        BenchmarkResult.arguments == args_db,
        BenchmarkResult.value.isnot(None),
    ).all()

    if not all_results:
        return {"error": "No BAR_GRAPH results found for this benchmark/config"}, 404

    by_system_vals = defaultdict(list)
    for r in all_results:
        by_system_vals[r.system_id].append(r.value)

    if system_id not in by_system_vals:
        return {"error": "Requested system_id has no results for this benchmark/config"}, 404

    y_raw_by_system = {sid: statistics.mean(vals) for sid, vals in by_system_vals.items()}
    y_norm_by_system = {sid: y_raw * y_flip for sid, y_raw in y_raw_by_system.items()}

    systems = System.query.filter(System.id.in_(list(y_raw_by_system.keys()))).all()
    comps_by_sid = {s.id: get_system_components(s) for s in systems}
    system_comps = comps_by_sid.get(system_id, {})

    # Observed system vs best system.
    system_y_raw = y_raw_by_system[system_id]
    system_y_norm = y_norm_by_system[system_id]
    best_system_norm = max(y_norm_by_system.values())
    worst_system_norm = min(y_norm_by_system.values())
    gap_to_best_system = best_system_norm - system_y_norm

    # Rank single-feature cohort mismatches.
    feature_explanations = []
    for feature_key in INSIGHT_COMPONENT_KEYS:
        value_to_norm_scores = defaultdict(list)  # component value -> [normalized y per system]
        systems_with_feature = set()

        for sid, y_norm in y_norm_by_system.items():
            v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
            if not v:
                continue
            systems_with_feature.add(sid)
            value_to_norm_scores[v].append(y_norm)

        total_systems_with_feature = len(systems_with_feature)
        if total_systems_with_feature < MIN_SYSTEMS_TOTAL:
            continue

        valid_values = []
        for v, norm_scores in value_to_norm_scores.items():
            n_systems_for_value = len(norm_scores)  # 1 score per system
            if n_systems_for_value < min_cohort_n:
                continue
            valid_values.append((v, statistics.mean(norm_scores), n_systems_for_value))

        if len(valid_values) < 2:
            continue

        system_value = (system_comps.get(feature_key) or '').strip()
        if not system_value:
            continue

        best_mean_norm = max(m for _, m, _ in valid_values)
        system_entry = next((e for e in valid_values if e[0] == system_value), None)
        if not system_entry:
            continue

        _, system_mean_norm, n_systems_for_value = system_entry
        delta_to_best_cohort = best_mean_norm - system_mean_norm
        if delta_to_best_cohort <= 0:
            continue

        feature_explanations.append({
            "feature_key": feature_key,
            "feature_label": label_map.get(feature_key, feature_key),
            "feature_value": system_value,
            "cohort_mean_normalized": system_mean_norm,
            "best_cohort_mean_normalized": best_mean_norm,
            "delta_to_best_cohort_normalized": delta_to_best_cohort,
            "n_systems_for_cohort_value": n_systems_for_value,
            "n_systems_with_feature": total_systems_with_feature,
        })

    feature_explanations.sort(key=lambda x: x["delta_to_best_cohort_normalized"], reverse=True)
    feature_explanations = feature_explanations[:top_n_components]

    pair_explanations = []
    if include_pairs:
        # Start with the pair concepts that map well to "CPU dominates vs GPU dominates".
        pair_defs = [
            ("processor", "memory"),
            ("processor", "cooler_model"),
            ("processor", "graphics"),
            ("graphics", "memory"),
        ]

        for k1, k2 in pair_defs:
            pair_to_norm_scores = defaultdict(list)  # (v1,v2) -> [normalized y per system]
            systems_with_pair = set()

            for sid, y_norm in y_norm_by_system.items():
                c1 = (comps_by_sid.get(sid, {}).get(k1) or '').strip()
                c2 = (comps_by_sid.get(sid, {}).get(k2) or '').strip()
                if not c1 or not c2:
                    continue
                systems_with_pair.add(sid)
                pair_to_norm_scores[(c1, c2)].append(y_norm)

            total_systems_with_pair = len(systems_with_pair)
            if total_systems_with_pair < MIN_SYSTEMS_TOTAL or len(pair_to_norm_scores) < 2:
                continue

            valid_pairs = []
            for pair_tuple, norm_scores in pair_to_norm_scores.items():
                n_systems_for_pair = len(norm_scores)
                if n_systems_for_pair < min_pair_n:
                    continue
                valid_pairs.append((pair_tuple, statistics.mean(norm_scores), n_systems_for_pair))

            if len(valid_pairs) < 2:
                continue

            s1 = (system_comps.get(k1) or '').strip()
            s2 = (system_comps.get(k2) or '').strip()
            if not s1 or not s2:
                continue

            best_pair_mean_norm = max(m for _, m, _ in valid_pairs)
            system_pair_entry = next((e for e in valid_pairs if e[0] == (s1, s2)), None)
            if not system_pair_entry:
                continue

            _, system_pair_mean_norm, n_systems_for_pair = system_pair_entry
            delta_to_best_pair_normalized = best_pair_mean_norm - system_pair_mean_norm
            if delta_to_best_pair_normalized <= 0:
                continue

            pair_explanations.append({
                "pair_keys": [k1, k2],
                "pair_label": f"{label_map.get(k1,k1)} + {label_map.get(k2,k2)}",
                "pair_values": [s1, s2],
                "pair_mean_normalized": system_pair_mean_norm,
                "best_pair_mean_normalized": best_pair_mean_norm,
                "delta_to_best_pair_normalized": delta_to_best_pair_normalized,
                "n_systems_for_pair": n_systems_for_pair,
                "n_systems_with_pair": total_systems_with_pair,
            })

        pair_explanations.sort(key=lambda x: x["delta_to_best_pair_normalized"], reverse=True)
        pair_explanations = pair_explanations[:top_n_pairs]

    return {
        "benchmark_title": title,
        "app_version": app_version,
        "args": args_analysis_key,
        "system_id": system_id,
        "direction": "higher_is_better_after_normalization",
        "y_flip": y_flip,
        "evidence_thresholds": {
            "min_cohort_n": min_cohort_n,
            "min_pair_n": min_pair_n,
            "min_systems_total_with_feature": MIN_SYSTEMS_TOTAL,
        },
        "observed": {
            "y_raw_mean": system_y_raw,
            "y_normalized_mean": system_y_norm,
            "best_system_y_normalized_mean": best_system_norm,
            "worst_system_y_normalized_mean": worst_system_norm,
            "gap_to_best_system_normalized": gap_to_best_system,
        },
        "single_feature_contributors": feature_explanations,
        "pair_contributors": pair_explanations,
    }, 200

@app.route('/insights')
def insights():
    # Exclude analysis rows that correspond only to non-primary BAR_GRAPH metrics (e.g. perf counters).
    primary_pairs = set(
        (b.title, b.app_version or '')
        for b in Benchmark.query.filter(
            Benchmark.display_format == 'BAR_GRAPH',
            Benchmark.is_primary.is_(True),
        ).all()
    )
    analyses = [
        a for a in BenchmarkAnalysis.query.order_by(BenchmarkAnalysis.benchmark_title, BenchmarkAnalysis.benchmark_app_version).all()
        if (a.benchmark_title, a.benchmark_app_version or '') in primary_pairs
    ]
    # Return as an array of structured mappings for the Jinja template
    return render_template('insights.html', analyses=analyses)

@app.route('/api/systems_for_benchmark')
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
    
    systems = System.query.filter(System.id.in_(sys_ids)).all()
    
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


@app.route('/api/systems_for_benchmark_title')
def api_systems_for_benchmark_title():
    """
    Return systems that have BAR_GRAPH primary results for a benchmark title/app_version/args.
    Used by the Performance Insights UI to populate a system dropdown.
    """
    title = (request.args.get('benchmark_title') or '').strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or '').strip()

    if not title:
        return {"error": "Missing benchmark_title query parameter"}, 400

    # Analyzer buckets empty args as the string 'default'
    args_db = ''
    if args_str and args_str.lower() != 'default':
        args_db = args_str

    bms_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == 'BAR_GRAPH',
        Benchmark.is_primary.is_(True),
    )
    if app_version:
        bms_q = bms_q.filter(Benchmark.app_version == app_version)

    primary_bms = bms_q.all()
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

    systems = System.query.filter(System.id.in_(sys_ids)).all()
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

@app.cli.command("init-db")
def init_db():
    """Initialize the database."""
    with app.app_context():
        db.create_all()
        print("Database initialized.")

@app.cli.command("ingest")
def ingest():
    """Ingest benchmarks from the benchmarks directory."""
    with app.app_context():
        bm_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'benchmarks')
        if os.path.exists(bm_dir):
            parse_benchmark_files(bm_dir)
        else:
            print(f"Benchmarks directory not found at {bm_dir}")


@app.cli.command("backfill-perf-counters")
def backfill_perf_counters():
    """Mark Linux perf counters as non-primary BAR_GRAPH metrics."""
    from sqlalchemy import func, or_

    with app.app_context():
        # Primary fix: perf counters often appear only in BenchmarkResult.arguments
        # (e.g. "perf page-faults defconfig"), while Benchmark.title/scale may look normal.
        # Join through results so we can find and fix existing data reliably.
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
            # fallback: match perf anywhere (helps if title has prefix text)
            func.lower(Benchmark.title).like('%perf %'),
            func.lower(Benchmark.title).like('%perf-%'),
            func.lower(Benchmark.description).like('%perf %'),
            func.lower(Benchmark.description).like('%perf-%'),
        )

        # Also include any perf-like metadata even if arguments don't include it.
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


@app.cli.command("rebuild-performance-insights")
def rebuild_performance_insights():
    """Recompute legacy Performance Insights (BenchmarkAnalysis cohort η²)."""
    with app.app_context():
        analyze_benchmarks()
        print("Legacy performance insights rebuilt.")


@app.cli.command("rebuild-all-insights")
def rebuild_all_insights():
    """Recompute legacy cohort stats and ML workload/attribution/thermal profiles."""
    with app.app_context():
        analyze_benchmarks()
        n = analyze_ml_profiles()
        print(f"Performance insights rebuilt (legacy + ML profiles for {n} record(s)).")


@app.cli.command("rebuild-ml-insights")
def rebuild_ml_insights():
    """Recompute ML workload/attribution/thermal profiles only."""
    with app.app_context():
        n = analyze_ml_profiles()
        print(f"ML profiles updated for {n} analysis record(s).")


@app.cli.command("debug-insights-coverage")
@click.option("--title", default="", help="Benchmark title substring (case-insensitive). Example: 'ONNX Runtime'")
@click.option("--app-version", default="", help="Exact benchmark app_version. Example: '1.24.1'")
def debug_insights_coverage(title, app_version):
    """
    Print distinct system coverage per (benchmark title, app_version, arguments)
    for BAR_GRAPH benchmarks. Useful for verifying why Performance Insights
    may be empty.
    """
    title_sub = (title or "").strip().lower()
    app_ver = (app_version or "").strip()

    with app.app_context():
        from sqlalchemy import func as _func

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


@app.cli.command("debug-insights-feature-values")
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
    from app.analyzer import INSIGHT_COMPONENT_KEYS
    from app.components import get_system_components

    title_sub = (title or "").strip().lower()
    app_ver = (app_version or "").strip()
    args_str = (args_value or "").strip()

    with app.app_context():
        # Find candidate BAR_GRAPH benchmarks matching title (+ optional app-version).
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
        # Gather systems that have non-null numeric results for the requested args.
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

        systems = System.query.filter(System.id.in_(sys_ids)).all()
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
            # Distinct values distribution
            dist = {}
            for sid, v in values_by_sys.items():
                dist.setdefault(v, set()).add(sid)
            items = sorted([(v, len(sids)) for v, sids in dist.items()], key=lambda x: -x[1])
            distinct_vals = len(items)
            print(f"- {fk}: distinct values={distinct_vals}, top={items[:3]}")


@app.cli.command("debug-insights-analysis-features")
@click.option("--title", required=True, help="Benchmark title substring (case-insensitive), e.g. 'Timed Linux Kernel Compilation'")
@click.option("--app-version", default="", help="Exact benchmark app_version, e.g. '6.15'")
@click.option("--args", "args_value", default="defconfig", help="Exact BenchmarkResult.arguments to inspect")
def debug_insights_analysis_features(title, app_version, args_value):
    """
    Prints whether Performance Insights produced non-error features for a given benchmark/config.
    Useful for debugging why /insights shows nothing.
    """
    from sqlalchemy import func as _func
    from app.models import BenchmarkAnalysis

    title_sub = (title or "").strip().lower()
    app_ver = (app_version or "").strip()
    args_str = (args_value or "").strip()

    with app.app_context():
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
                    n = first.get("n") if isinstance(first, dict) else None
                    print(f"  [OK ] {feat_key}: first='{name}' n={n}")
                shown += 1


@app.cli.command("debug-insights-summary")
def debug_insights_summary():
    """
    Print a high-level summary of Performance Insights stored in BenchmarkAnalysis:
    - which DB path this process is using
    - number of analysis rows
    - how many analyses contain at least one non-error feature value (what the /insights
      template uses to decide whether to render cards vs the fallback message)
    """
    from app.models import BenchmarkAnalysis

    with app.app_context():
        print("SQLALCHEMY_DATABASE_URI:", app.config.get("SQLALCHEMY_DATABASE_URI"))
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


@app.cli.command("debug-insights-perf-args")
def debug_insights_perf_args():
    """
    Check whether Performance Insights analysis_json still contains perf-like
    BenchmarkResult.arguments keys (e.g. 'perf page-faults ...').
    """
    from app.models import BenchmarkAnalysis

    perf_arg_hits = defaultdict(int)  # args_key -> count analyses mentioning it
    analyses_with_perf = 0

    with app.app_context():
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
        for args_key, n in top:
            print(f"- args='{args_key}': analyses={n}")


@app.cli.command("debug-primary-perf-benchmarks")
def debug_primary_perf_benchmarks():
    """
    Print BAR_GRAPH benchmarks marked primary that look perf-like.
    If this list is non-empty, perf counters will still appear in insights after rebuild.
    """
    from app.models import Benchmark

    with app.app_context():
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


@app.cli.command("import-hardware-ranks")
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
    import json
    from pathlib import Path
    from app.components import hardware_rank_match_key

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

    with app.app_context():
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


@app.cli.command("sync-openbenchmarking-cache")
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
    from app.ob_cache_sync import (
        build_ob_cache_index,
        default_ob_cache_dir,
        default_pts_clone_dir,
        sync_ob_cache,
    )

    lp = local_path.strip() or None
    with app.app_context():
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


@app.cli.command("sync-hardware-ranks-api")
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
    from app.hardware_ranks_api_sync import build_rank_entries_from_api, upsert_theoretical_ranks

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
    with app.app_context():
        ct = upsert_theoretical_ranks(entries)
        db.session.commit()
        print(
            "hardware_theoretical_ranks:",
            ct["added"], "inserted,",
            ct["updated"], "updated.",
        )


@app.cli.command("calibrate-hardware-ranks")
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
    from app.hardware_ranks_calibrate import calibrate_hardware_ranks

    with app.app_context():
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


if __name__ == '__main__':
    # Debug reloader spawns a second process and locks SQLite; off by default for systemd installs.
    debug = os.environ.get('BENCHVIZ_DEBUG', '').lower() in ('1', 'true', 'yes')
    app.run(debug=debug, use_reloader=debug, host='0.0.0.0', port=8765)


@app.cli.command("debug-pool-args")
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
    from app.args_pooling import (
        parse_pool_flags,
        parse_args_tokens,
        extract_flag_values,
        pool_key_for_args_by_flags,
    )

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


@app.cli.command("debug-pool-axes")
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
    from app.args_pooling import (
        parse_pool_flags,
        extract_flag_values,
    )

    with app.app_context():
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

        # Extract pool-flag values for each raw args (use first extracted value like api_compare).
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

        # Common values -> one axis per raw args string.
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
        print("")
