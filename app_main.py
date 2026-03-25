from app import create_app, db
from app.models import System, Benchmark, BenchmarkResult, SystemNvmeConfig, BenchmarkAnalysis, SavedComparison
from app.parser import parse_benchmark_files, parse_file
from app.analyzer import analyze_benchmarks
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
                except Exception as e:
                    print(f"Error in background benchmark analysis: {e}")
                    
        threading.Thread(target=run_analysis_with_context, args=(app.app_context(),)).start()
        
        if extracted_xml_count > 0:
            flash(f'Successfully ingested {extracted_xml_count} benchmark records.', 'success')
            shutil.rmtree(temp_dir, ignore_errors=True)
            return redirect(url_for('dashboard'))
        else:
            flash('No valid XML benchmark files were found in the upload.', 'error')
            
    except Exception as e:
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
    db.session.commit()
    flash(f'System "{system_name}" successfully deleted.', 'success')
    return redirect(url_for('dashboard'))

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

    comparison_groups = []

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

        if args_filter is not None:
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

        # Track non-empty primary arguments for this benchmark so we can
        # associate sensor runs even when the primary run's arguments are empty.
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
            if args_val is None or (isinstance(args_val, str) and args_val.strip() == ""):
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
                    metric_label = bm.scale or (bm.description or "Primary Result")[:50]
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
            sensor_keywords = ('temperature', 'frequency', 'usage', 'power', 'celsius', 'mhz', 'watts')
            sensors = [s for s in sensors if s.description and any(k in s.description.lower() for k in sensor_keywords)]

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
                                trace["stats"] = stats_dict

                    s_traces.append(trace)

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

                    charts.append({
                        "metric": metric_label,
                        "description": s_bm.description,
                        "scale": s_bm.scale,
                        "display_format": s_bm.display_format,
                        "proportion": s_bm.proportion,
                        "traces": s_traces,
                        "is_primary": False
                    })

            if charts:
                charts.sort(key=lambda x: not x["is_primary"])
                title = f"{primary_benchmark.title} ({primary_benchmark.app_version})"
                if args_val and (isinstance(args_val, str) and args_val.strip()):
                    title += f" — {args_val}"
                # Compute display label for this run so sensor data is explicitly correlated
                # (e.g. "Unix Makefiles" for empty-args run when other option is "Ninja").
                args_label = None
                if args_val and (isinstance(args_val, str) and args_val.strip()):
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
                comparison_groups.append({
                    "title": title,
                    "charts": charts,
                    "system_details": system_details,
                    "args": args_val if args_val is not None else "",
                    "args_label": args_label or args_val or "",
                })

    if not comparison_groups:
        return {"error": "Could not find benchmark data"}, 404
        
    return {"comparison_groups": comparison_groups}


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

    For a feature_key (e.g. 'processor'), we:
      1) Group systems by their component value.
      2) Compute within-bucket spread of the benchmark (how much performance varies
         even when the component is held constant).
      3) Compare the bucket spread to the overall spread across all systems.

    A higher reduction_ratio => holding that component constant reduces variability
    more => the component is more explanatory for this benchmark/config.
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

    # Feature scope inference: restrict candidate keys to reduce confounding.
    # We infer whether the benchmark is CPU/GPU/storage heavy based on the benchmark text,
    # and then only rank keys that are plausibly relevant to that scope.
    text_blob = " ".join([
        (rep_bm.title or ""),
        (rep_bm.description or ""),
        args_str or "",
    ]).lower()

    cpu_scoped_keys = {
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
    }
    gpu_scoped_keys = {
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
    }
    storage_scoped_keys = {
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
    }

    scope = "general"
    if ("kernel" in text_blob and ("build" in text_blob or "compil" in text_blob or "make" in text_blob or "gcc" in text_blob)) or \
       ("compil" in text_blob and ("linux" in text_blob)):
        scope = "cpu"
    elif any(k in text_blob for k in ["vulkan", "cuda", "opengl", "render", "graphics", "gpu "]):
        scope = "gpu"
    elif any(k in text_blob for k in ["nvme", "disk", "io", "i/o", "storage", "ssd", "hdd", "throughput"]):
        scope = "storage"

    # Optional override for debugging / experimentation.
    scope_override = (request.args.get('scope') or '').strip().lower()
    if scope_override in {"all", "general"}:
        scope = "general"
    elif scope_override in {"cpu", "gpu", "storage"}:
        scope = scope_override

    if scope == "cpu":
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k in cpu_scoped_keys]
    elif scope == "gpu":
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k in gpu_scoped_keys]
    elif scope == "storage":
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k in storage_scoped_keys]
    else:
        allowed_singles = list(INSIGHT_COMPONENT_KEYS)

    if not allowed_singles:
        allowed_singles = list(INSIGHT_COMPONENT_KEYS)

    rows = []

    def eval_single(feature_key):
        # Group by component value => bucket -> list of y_norm per system.
        buckets = defaultdict(list)
        systems_with_nonempty = 0
        for sid in sys_ids:
            v = (comps_by_sid.get(sid, {}).get(feature_key) or '').strip()
            if not v:
                continue
            systems_with_nonempty += 1
            buckets[v].append(y_norm_by_system[sid])

        if systems_with_nonempty < MIN_SYSTEMS_TOTAL:
            return

        qualifying_buckets = [(v, ys) for v, ys in buckets.items() if len(ys) >= min_cohort_n]
        if len(qualifying_buckets) < min_distinct_cohorts:
            return

        bucket_spreads = []
        for v, ys in qualifying_buckets:
            s = robust_spread(ys)
            bucket_spreads.append((v, s, len(ys)))

        if len(bucket_spreads) < min_distinct_cohorts:
            return

        # Weighted by bucket size so big buckets matter more.
        total_n = sum(n for _, _, n in bucket_spreads) or 1
        conditional_spread = sum(s * n for _, s, n in bucket_spreads) / total_n

        reduction_ratio = 1.0 - (conditional_spread / overall_spread_eps)

        rows.append({
            "feature_type": "single",
            "feature_key": feature_key,
            "feature_label": label_map.get(feature_key, feature_key),
            "reduction_ratio": reduction_ratio,
            "overall_spread": overall_spread,
            "conditional_spread": conditional_spread,
            "distinct_cohort_values": len(bucket_spreads),
            "systems_with_feature": systems_with_nonempty,
            "min_cohort_n": min_cohort_n,
        })

    def eval_pair(k1, k2):
        buckets = defaultdict(list)  # (v1,v2) -> [y_norm]
        systems_with_pair = 0
        for sid in sys_ids:
            c1 = (comps_by_sid.get(sid, {}).get(k1) or '').strip()
            c2 = (comps_by_sid.get(sid, {}).get(k2) or '').strip()
            if not c1 or not c2:
                continue
            systems_with_pair += 1
            buckets[(c1, c2)].append(y_norm_by_system[sid])

        if systems_with_pair < MIN_SYSTEMS_TOTAL:
            return

        qualifying = [(pair, ys) for pair, ys in buckets.items() if len(ys) >= min_cohort_n]
        if len(qualifying) < min_distinct_cohorts:
            return

        bucket_spreads = []
        for pair, ys in qualifying:
            s = robust_spread(ys)
            bucket_spreads.append((pair, s, len(ys)))

        if len(bucket_spreads) < min_distinct_cohorts:
            return

        total_n = sum(n for _, _, n in bucket_spreads) or 1
        conditional_spread = sum(s * n for _, s, n in bucket_spreads) / total_n
        reduction_ratio = 1.0 - (conditional_spread / overall_spread_eps)

        rows.append({
            "feature_type": "pair",
            "feature_key": f"{k1}+{k2}",
            "feature_label": f"{label_map.get(k1,k1)} + {label_map.get(k2,k2)}",
            "reduction_ratio": reduction_ratio,
            "overall_spread": overall_spread,
            "conditional_spread": conditional_spread,
            "distinct_cohort_values": len(bucket_spreads),
            "systems_with_feature": systems_with_pair,
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

    rows.sort(key=lambda r: r["reduction_ratio"], reverse=True)
    rows = rows[:top_k]

    return {
        "rows": rows,
        "meta": {
            "benchmark_title": title,
            "app_version": app_version,
            "args": args_analysis_key,
            "y_label": y_label_base,
            "x_label": "conditional within-bucket spread (lower is better)",
            "overall_spread": overall_spread,
            "overall_spread_eps": overall_spread_eps,
            "min_cohort_n": min_cohort_n,
            "min_distinct_cohorts": min_distinct_cohorts,
            "include_pairs": include_pairs,
            "feature_scope": scope,
        }
    }, 200


@app.route('/api/variance_leaderboard_coverage')
def api_variance_leaderboard_coverage():
    """
    Returns benchmark+args keys that have enough evidence to produce leaderboard rows.

    Evidence is checked using stored BenchmarkAnalysis.analysis_json:
    for each args bucket, if ANY feature has at least `min_distinct_cohorts`
    cohort values with `n >= min_cohort_n`, we consider that (benchmark,args)
    selectable for the variance leaderboard.
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
    for a in analyses:
        if not a.analysis_json:
            continue
        b_title = a.benchmark_title
        b_app = a.benchmark_app_version or ''

        for args_key, feature_stats in (a.analysis_json or {}).items():
            if not isinstance(feature_stats, dict):
                continue

            has_any_feature = False
            for feature_key, feature_values in feature_stats.items():
                # feature_values is expected to be a list of val_stat dicts or [{error:...}]
                if not isinstance(feature_values, list) or not feature_values:
                    continue
                if feature_values[0].get('error'):
                    continue

                # feature_values are cohort-value entries
                qualified = [v for v in feature_values if isinstance(v, dict) and (v.get('n') or 0) >= min_cohort_n]
                if len(qualified) >= min_distinct_cohorts:
                    has_any_feature = True
                    break

            if has_any_feature:
                buckets[(b_title, b_app)].add(args_key)

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
    """Recompute Performance Insights (BenchmarkAnalysis) for all BAR_GRAPH benchmarks."""
    with app.app_context():
        analyze_benchmarks()
        print("Performance Insights rebuilt.")


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


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8765)
