import os
import glob
import re
import json
from lxml import etree
from . import db
from .models import System, BenchmarkResult
from .benchmark_util import get_or_create_benchmark
from .system_util import resolve_system_for_import

STRING_PROFILE_FIELDS = (
    'primary_system_name',
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
    try:
        # Phoronix XML can sometimes have malformed chars, so we use lxml with recover=True
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
        
        # 1. UPSERT SYSTEM (same identifier + different hardware → disambiguated name)
        system_lookup_map = {}

        system, _, system_note = resolve_system_for_import(
            system_id,
            main_hardware,
            system_node.findtext('Software', default=''),
            system_node.findtext('User', default=''),
            system_node.findtext('TimeStamp', default=''),
        )
        if system_note:
            print(system_note)
            _import_notes.append(system_note)

        apply_system_profile(system, system_profile)

        # Keep a mapping of entry identifiers to system objects for the run
        system_lookup_map[system_id] = system

        # 2. PROCESS RESULTS
        current_identifier = ""
        for result_node in root.findall('Result'):
            raw_ident = result_node.findtext('Identifier', default='')
            if raw_ident:
                import re
                # Match Phoronix format ending in vX.Y.Z or X.Y.Z and strip the Z (patch)
                # e.g., pts/build-linux-kernel-1.17.1 -> pts/build-linux-kernel-1.17
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
            
            # Upsert benchmark definition (unique on identifier/title/version/description/scale).
            benchmark = get_or_create_benchmark(
                identifier=current_identifier,
                title=title,
                app_version=app_version,
                description=description,
                scale=scale,
                proportion=proportion,
                display_format=display_format,
                # BAR_GRAPH is usually the primary benchmark result, but Linux perf counters are
                # "sensor-like" metrics and shouldn't be treated as primary results.
                is_primary=(display_format == 'BAR_GRAPH' and not is_perf_counter),
            )
                
            # Extract data. Data could be multiple Entries (e.g., if multiple systems were present in the XML)
            # But usually it's one Entry for the current system.
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
                        )
                        if entry_note:
                            print(entry_note)
                            _import_notes.append(entry_note)
                        apply_system_profile(entry_system, system_profile)
                        system_lookup_map[entry_identifier] = entry_system
                        
                    value_str = entry_node.findtext('Value')
                    
                    # Check if this specific result already exists (System + Benchmark Object + Arguments)
                    # Benchmark objects are already uniquely keyed by identifier + scale above.
                    b_result = BenchmarkResult.query.filter_by(
                        system_id=entry_system.id,
                        benchmark_id=benchmark.id,
                        arguments=arguments
                    ).first()
                    
                    if not b_result:
                        b_result = BenchmarkResult(
                            system_id=entry_system.id,
                            benchmark_id=benchmark.id,
                            arguments=arguments
                        )
                        db.session.add(b_result)
                    
                    if benchmark.display_format == 'BAR_GRAPH':
                        try:
                            b_result.value = float(value_str)
                        except (ValueError, TypeError):
                            b_result.value = None
                        # Also capture per-run values (variability within system).
                        # Phoronix often stores colon-separated run values in either:
                        #  - <RawString>...</RawString>
                        #  - or <JSON>{"test-run-times":"a:b:c"}</JSON>
                        raw_run_str = entry_node.findtext('RawString', default='') or ''
                        run_values = []
                        if raw_run_str.strip():
                            # Extract all numeric tokens (supports ints/floats/exponents).
                            toks = re.findall(r'[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?', raw_run_str)
                            for t in toks:
                                try:
                                    run_values.append(float(t))
                                except (ValueError, TypeError):
                                    pass
                        if not run_values:
                            json_text = entry_node.findtext('JSON', default='') or ''
                            if json_text.strip():
                                try:
                                    parsed = json.loads(json_text)
                                    if isinstance(parsed, dict):
                                        # Prefer the canonical key when present.
                                        candidate_keys = [
                                            k for k in parsed.keys()
                                            if isinstance(k, str) and ('test-run-times' in k or 'run-times' in k or 'run_times' in k)
                                        ]
                                        for ck in candidate_keys:
                                            v = parsed.get(ck)
                                            if isinstance(v, str) and v.strip():
                                                toks = re.findall(r'[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?', v)
                                                for t in toks:
                                                    try:
                                                        run_values.append(float(t))
                                                    except (ValueError, TypeError):
                                                        pass
                                                if run_values:
                                                    break
                                except Exception:
                                    pass
                        # Persist run values only if we extracted something useful.
                        if run_values:
                            b_result.data_json = run_values
                    elif benchmark.display_format == 'LINE_GRAPH':
                        # Line graphs are comma separated values
                        try:
                            values = [float(v.strip()) for v in value_str.split(',') if v.strip()]
                            b_result.data_json = values
                        except (ValueError, TypeError, AttributeError):
                            b_result.data_json = None

    except Exception as e:
        print(f"Error parsing {file_path}: {e}")
