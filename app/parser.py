import os
import glob
import re
import uuid
from lxml import etree
from . import db
from .models import System, BenchmarkResult
from .benchmark_util import get_or_create_benchmark
from .profile_snapshot import capture_profile_snapshot
from .result_merge import assign_bar_graph_result, assign_line_graph_result
from .system_util import resolve_system_for_import

STRING_PROFILE_FIELDS = (
    'primary_system_name',
    'serial_number',
    'chassis_version',
    'custom_hardware',
    'cooler_model',
    'psu_model',
    'psu_wattage',
    'manual_notes',
)

BOOL_PROFILE_FIELDS = (
    'external_off',
    'gpu_fans',
    'memory_fans',
    'nvme_fans',
)

_import_notes: list[str] = []


def pop_import_notes() -> list[str]:
    """Return and clear notes collected during the current import batch."""
    notes = list(_import_notes)
    _import_notes.clear()
    return notes

def apply_system_profile(system, system_profile):
    if not system_profile:
        return

    for field in STRING_PROFILE_FIELDS:
        value = system_profile.get(field)
        if value is not None and value != '':
            setattr(system, field, value)

    for field in BOOL_PROFILE_FIELDS:
        if field in system_profile and system_profile[field] is not None:
            setattr(system, field, bool(system_profile[field]))

    if not system.primary_system_name:
        system.primary_system_name = system.identifier

def parse_benchmark_files(directory, system_profile=None):
    """
    Parses all XML benchmark files in the given directory and ingests
    them into the SQLite database.
    """
    files = glob.glob(os.path.join(directory, '*.xml'))
    count = 0
    
    for file_path in files:
        parse_file(file_path, system_profile=system_profile)
        count += 1
        
    db.session.commit()
    print(f"Successfully processed {count} benchmark files.")

def parse_file(file_path, system_profile=None):
    print(f"Processing {file_path}...")
    import_batch_id = str(uuid.uuid4())
    try:
        parser = etree.XMLParser(recover=True)
        tree = etree.parse(file_path, parser)
        root = tree.getroot()
        
        if root.tag != 'PhoronixTestSuite':
            print(f"Skipping {file_path}: Not a Phoronix Test Suite file.")
            return

        system_node = root.find('System')
        if system_node is None:
            print(f"Skipping {file_path}: No System node found.")
            return

        system_id = system_node.findtext('Identifier', default='')
        main_hardware = system_node.findtext('Hardware', default='')
        
        system_lookup_map = {}

        import_serial = (system_profile or {}).get('serial_number') if system_profile else None

        system, _, system_note = resolve_system_for_import(
            system_id,
            main_hardware,
            system_node.findtext('Software', default=''),
            system_node.findtext('User', default=''),
            system_node.findtext('TimeStamp', default=''),
            serial_number=import_serial,
        )
        if system_note:
            print(system_note)
            _import_notes.append(system_note)

        apply_system_profile(system, system_profile)

        system_lookup_map[system_id] = system

        current_identifier = ""
        failed_test_args: set[str] = set()
        for result_node in root.findall('Result'):
            raw_ident = result_node.findtext('Identifier', default='')
            if raw_ident:
                match = re.search(r'-(\d+\.\d+)\.\d+$', raw_ident)
                if match:
                    current_identifier = raw_ident[:match.start()] + '-' + match.group(1)
                else:
                    current_identifier = raw_ident
                    
            title = result_node.findtext('Title', default='')
            app_version = result_node.findtext('AppVersion', default='')
            description = result_node.findtext('Description', default='')
            
            scale = result_node.findtext('Scale', default='')
            proportion = result_node.findtext('Proportion', default='')
            display_format = result_node.findtext('DisplayFormat', default='')
            arguments = result_node.findtext('Arguments', default='')
            args_l = (arguments or '').strip().lower()
            title_l = (title or '').strip().lower()
            scale_l = (scale or '').strip().lower()
            ident_l = (current_identifier or '').strip().lower()
            desc_l = (description or '').strip().lower()
            is_perf_counter = (
                ident_l.startswith('perf') or
                title_l.startswith('perf ') or title_l.startswith('perf-') or
                desc_l.startswith('perf ') or desc_l.startswith('perf-') or
                args_l.startswith('perf ') or args_l.startswith('perf-') or
                scale_l.startswith('perf')
            )
            
            benchmark = get_or_create_benchmark(
                identifier=current_identifier,
                title=title,
                app_version=app_version,
                description=description,
                scale=scale,
                proportion=proportion,
                display_format=display_format,
                is_primary=(display_format == 'BAR_GRAPH' and not is_perf_counter),
            )

            # For LINE_GRAPH (MONITOR) results, check if the test config belongs
            # to a failed test and skip if so. MONITOR arguments embed the test
            # config after the sensor name prefix.
            if display_format == 'LINE_GRAPH':
                if arguments.strip() and failed_test_args:
                    desc = (description or '').strip()
                    if desc.endswith(' Monitor'):
                        sensor_name = desc[:-len(' Monitor')].strip()
                        if sensor_name and arguments.strip().startswith(sensor_name):
                            test_config = arguments.strip()[len(sensor_name):].strip()
                            if test_config in failed_test_args:
                                continue
                
            data_node = result_node.find('Data')
            if data_node is not None:
                for entry_node in data_node.findall('Entry'):
                    entry_identifier = entry_node.findtext('Identifier', default='')
                    entry_system = None
                    if entry_identifier in system_lookup_map:
                        entry_system = system_lookup_map[entry_identifier]
                    else:
                        entry_system, _, entry_note = resolve_system_for_import(
                            entry_identifier,
                            '',
                            '',
                            '',
                            '',
                            fallback_hardware=main_hardware,
                            serial_number=import_serial,
                        )
                        if entry_note:
                            print(entry_note)
                            _import_notes.append(entry_note)
                        apply_system_profile(entry_system, system_profile)
                        system_lookup_map[entry_identifier] = entry_system
                        
                    value_str = entry_node.findtext('Value')
                    profile_snapshot = capture_profile_snapshot(entry_system)

                    # For BAR_GRAPH results, check if the test failed and skip
                    # the row entirely if so.
                    if display_format == 'BAR_GRAPH':
                        value_empty = not (value_str or '').strip()
                        if value_empty:
                            json_text = entry_node.findtext('JSON', default='') or ''
                            if json_text.strip():
                                try:
                                    parsed = json.loads(json_text)
                                    if isinstance(parsed, dict):
                                        error_val = parsed.get('error')
                                        if error_val and isinstance(error_val, str) and error_val.strip():
                                            # Track the failed test config so
                                            # associated MONITOR data is skipped.
                                            if arguments.strip():
                                                failed_test_args.add(arguments.strip())
                                            continue
                                except Exception:
                                    pass

                    b_result = BenchmarkResult(
                        system_id=entry_system.id,
                        benchmark_id=benchmark.id,
                        arguments=arguments,
                        import_batch_id=import_batch_id,
                        profile_snapshot=profile_snapshot,
                    )
                    db.session.add(b_result)
                    
                    if benchmark.display_format == 'BAR_GRAPH':
                        assign_bar_graph_result(b_result, entry_node, value_str)
                    elif benchmark.display_format == 'LINE_GRAPH':
                        try:
                            values = [float(v.strip()) for v in (value_str or '').split(',') if v.strip()]
                        except (ValueError, TypeError, AttributeError):
                            values = []
                        if values:
                            assign_line_graph_result(b_result, values)

        print(f"  Import batch {import_batch_id[:8]}… stored as distinct observation run(s).")

    except Exception as e:
        print(f"Error parsing {file_path}: {e}")
