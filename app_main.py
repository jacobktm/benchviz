from app import create_app, db
from app.models import System, Benchmark, BenchmarkResult, SystemNvmeConfig, BenchmarkAnalysis
from app.parser import parse_benchmark_files, parse_file
from app.analyzer import analyze_benchmarks
from flask import render_template, request, redirect, url_for, flash
from urllib.parse import unquote
import os
import threading
import zipfile
import tempfile
import shutil
import statistics
import json
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

    primary_benchmarks = Benchmark.query.filter_by(display_format='BAR_GRAPH').all()
    
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
    
    # Group results by title and app_version
    grouped_results = {}
    for result in system.results:
        b = result.benchmark
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

@app.route('/compare')
def compare():
    systems_raw = System.query.all()
    # Apply standard metadata formatting
    systems = []
    for sys in systems_raw:
        sys.primary_group_name = get_primary_group_name(sys)
        sys.profile_label = format_system_profile_label(sys)
        systems.append(sys)
        
    # Sort alphabetically by base identifier
    systems.sort(key=lambda s: s.identifier)
    
    return render_template('compare.html', systems=systems)

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
            args_list = [r[0] for r in distinct_rows]
            if not args_list:
                continue

        for args_val in args_list:
            charts = []
            primary_traces = []
            sys_args_map = {}
            system_details = []
            primary_args_set = set()

            for sys_id in sys_id_ints:
                system = db.session.get(System, sys_id)
                if not system:
                    continue

                q = BenchmarkResult.query.filter(
                    BenchmarkResult.system_id == sys_id,
                    BenchmarkResult.benchmark_id.in_(matching_primary_bm_ids),
                )
                if args_val is None or (isinstance(args_val, str) and args_val.strip() == ""):
                    q = q.filter(
                        (BenchmarkResult.arguments.is_(None)) | (BenchmarkResult.arguments == "")
                    )
                else:
                    q = q.filter(BenchmarkResult.arguments == args_val)
                prim_res = q.first()

                if not prim_res:
                    continue

                sys_args_map[sys_id] = prim_res.arguments
                if prim_res.arguments:
                    primary_args_set.add(prim_res.arguments.strip())

                system_label = format_system_profile_label(system)
                short_name = system.identifier

                if not any(s['id'] == sys_id for s in system_details):
                    system_details.append({
                        'id': sys_id,
                        'short_name': short_name,
                        'full_label': system_label
                    })

                trace = {
                    "name": short_name,
                    "type": "bar" if primary_benchmark.display_format == "BAR_GRAPH" else "scatter",
                    "customdata": [system_label],
                    "hovertemplate": "%{customdata[0]}<br>%{x}<extra></extra>" if primary_benchmark.display_format == "BAR_GRAPH" else None
                }

                if primary_benchmark.display_format == "BAR_GRAPH":
                    trace["x"] = [short_name]
                    trace["y"] = [prim_res.value]
                elif primary_benchmark.display_format == "LINE_GRAPH":
                    y_data = prim_res.data_json or []
                    trace["x"] = list(range(len(y_data)))
                    trace["y"] = y_data
                    trace["mode"] = "lines"

                primary_traces.append(trace)

            if primary_traces:
                charts.append({
                    "metric": "Primary Result",
                    "description": primary_benchmark.description,
                    "scale": primary_benchmark.scale,
                    "display_format": primary_benchmark.display_format,
                    "proportion": primary_benchmark.proportion,
                    "options": sorted(primary_args_set),
                    "traces": primary_traces,
                    "is_primary": True
                })

            sensors = Benchmark.query.filter(
                Benchmark.title == primary_benchmark.title,
                Benchmark.app_version == primary_benchmark.app_version,
                Benchmark.display_format == 'LINE_GRAPH',
            ).all()

            for s_bm in sensors:
                s_traces = []
                sensor_bm_ids = [s.id for s in sensors]
                for sys_id in sys_args_map:
                    target_args = sys_args_map[sys_id]
                    system = db.session.get(System, sys_id)

                    all_s_res = BenchmarkResult.query.filter(
                        BenchmarkResult.system_id == sys_id,
                        BenchmarkResult.benchmark_id.in_(sensor_bm_ids),
                    ).all()
                    matching_s_res = [r for r in all_s_res if target_args and target_args in (r.arguments or "")]

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
                comparison_groups.append({
                    "title": title,
                    "charts": charts,
                    "system_details": system_details
                })

    if not comparison_groups:
        return {"error": "Could not find benchmark data"}, 404
        
    return {"comparison_groups": comparison_groups}

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
        config_rows = db.session.query(BenchmarkResult.arguments).filter(
            BenchmarkResult.benchmark_id.in_(bm_ids),
            BenchmarkResult.system_id.in_(sys_id_ints)
        ).distinct().all()
        configs = [r[0] or "" for r in config_rows if (r[0] or "").strip()]
        unique_common_suites[key] = {
            'id': key_to_one_bm_id[key],
            'label': f"{key[0]} ({key[1]})",
            'configs': configs
        }

    output_list = sorted(list(unique_common_suites.values()), key=lambda x: x['label'])

    return {"benchmarks": output_list}

@app.route('/insights')
def insights():
    analyses = BenchmarkAnalysis.query.order_by(BenchmarkAnalysis.benchmark_title, BenchmarkAnalysis.benchmark_app_version).all()
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

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8765)
