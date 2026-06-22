from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from app import db
from app.models import HardwareSpec, SpecFieldSchema, System


# ---------------------------------------------------------------------------
# Parsing helpers — extract structured info from PTS hardware strings
# ---------------------------------------------------------------------------

_CPU_CORES_RE = re.compile(r'(\d+)\s*[-]?\s*Core[s]?\s*', re.IGNORECASE)
_CPU_THREADS_RE = re.compile(r'(\d+)\s*[-]?\s*Thread[s]?\s*', re.IGNORECASE)
_CPU_CLOCK_RE = re.compile(r'@\s*([\d.]+)\s*GHz', re.IGNORECASE)
_CPU_CLOCK_MHZ_RE = re.compile(r'@\s*([\d.]+)\s*MHz', re.IGNORECASE)

_GPU_VRAM_MB_RE = re.compile(r'(\d+)\s*MB', re.IGNORECASE)
_GPU_VRAM_GB_RE = re.compile(r'(\d+)\s*GB', re.IGNORECASE)

_MEMORY_SIZE_GB_RE = re.compile(r'(\d+)\s*GB', re.IGNORECASE)
_MEMORY_SIZE_MB_RE = re.compile(r'(\d+)\s*MB', re.IGNORECASE)


def _first_hw_component(hardware_str: str, *prefixes: str) -> str:
    """Return the value for the first matching prefix in a PTS hardware string."""
    text = hardware_str.replace('\n', ',')
    for prefix in prefixes:
        for part in text.split(','):
            part = part.strip()
            if part.lower().startswith(prefix.lower() + ':'):
                return part.split(':', 1)[1].strip()
    return ''


def parse_cpu_spec(hardware_str: str) -> dict[str, Any]:
    """
    Build a cpu_spec JSON dict from a PTS hardware string.

    Flat extracts: cpu_model, cpu_cores, cpu_threads (for the top-level columns).
    JSON blob: { arch_family, clusters[], boost_clock_mhz, l2_cache_kb, l3_cache_kb }
    """
    if not hardware_str:
        return {}
    processor = _first_hw_component(hardware_str, 'Processor', 'CPU', 'CPU Model')
    if not processor:
        return {}

    spec: dict[str, Any] = {}
    flat: dict[str, Any] = {}

    flat['cpu_model'] = processor

    cores = _CPU_CORES_RE.search(processor)
    if cores:
        flat['cpu_cores'] = int(cores.group(1))

    threads = _CPU_THREADS_RE.search(processor)
    if threads:
        flat['cpu_threads'] = int(threads.group(1))

    clock = _CPU_CLOCK_RE.search(processor)
    if clock:
        spec['boost_clock_mhz'] = round(float(clock.group(1)) * 1000, 0)
    else:
        clock_mhz = _CPU_CLOCK_MHZ_RE.search(processor)
        if clock_mhz:
            spec['boost_clock_mhz'] = float(clock_mhz.group(1))

    # Clean model name
    clean = re.split(r'\s*@\s*', processor, maxsplit=1)[0].strip()
    clean = _CPU_CORES_RE.sub('', clean).strip()
    clean = _CPU_THREADS_RE.sub('', clean).strip()
    clean = re.sub(r'\s+', ' ', clean).strip()
    spec['model'] = clean

    # Single flat cluster (most PTS strings describe one homogenous CPU)
    cluster: dict[str, Any] = {'type': 'performance'}
    if flat.get('cpu_cores'):
        cluster['cores'] = flat['cpu_cores']
    if flat.get('cpu_threads'):
        cluster['threads'] = flat['cpu_threads']
    if spec.get('boost_clock_mhz'):
        cluster['boost_clock_mhz'] = spec['boost_clock_mhz']
    spec['clusters'] = [cluster]

    return {'_flat': flat, '_spec': spec}


def parse_gpu_spec(hardware_str: str) -> dict[str, Any]:
    """
    Build a gpu_spec JSON dict from a PTS hardware string.

    Flat: gpu_model.  JSON blob: { vram_mb, boost_clock_mhz, model }
    """
    if not hardware_str:
        return {}
    graphics = _first_hw_component(hardware_str, 'Graphics', 'GPU', 'Graphics Processor')
    if not graphics:
        return {}

    spec: dict[str, Any] = {}
    flat: dict[str, Any] = {}
    flat['gpu_model'] = graphics

    vram_gb = _GPU_VRAM_GB_RE.search(graphics)
    if vram_gb:
        spec['vram_mb'] = int(vram_gb.group(1)) * 1024
    elif _GPU_VRAM_MB_RE.search(graphics):
        vram_mb = _GPU_VRAM_MB_RE.search(graphics)
        if vram_mb:
            spec['vram_mb'] = int(vram_mb.group(1))

    clock = _CPU_CLOCK_RE.search(graphics)
    if clock:
        spec['boost_clock_mhz'] = round(float(clock.group(1)) * 1000, 0)

    clean = re.split(r'\s*@\s*', graphics, maxsplit=1)[0].strip()
    clean = re.sub(r'\s*\d+\s*GB\s*$', '', clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r'\s+\d+\s*MB\s*$', '', clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r'\s+GPU\s*$', '', clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r'\s+', ' ', clean).strip()
    spec['model'] = clean

    return {'_flat': flat, '_spec': spec}


def parse_memory_spec(hardware_str: str) -> dict[str, Any]:
    """
    Build a memory_spec JSON dict from a PTS hardware string.
    Returns empty dict if no memory data found.
    """
    if not hardware_str:
        return {}
    memory = _first_hw_component(hardware_str, 'Memory', 'RAM', 'System Memory')
    if not memory:
        return {}

    spec: dict[str, Any] = {'raw': memory}

    size_mb_match = _MEMORY_SIZE_MB_RE.search(memory)
    size_gb_match = _MEMORY_SIZE_GB_RE.search(memory)
    if size_mb_match:
        spec['size_mb'] = int(size_mb_match.group(1))
    elif size_gb_match:
        spec['size_mb'] = int(size_gb_match.group(1)) * 1024

    spd = re.search(r'@\s*(\d+)\s*MHz', memory, re.IGNORECASE)
    if spd:
        spec['speed_mhz'] = int(spd.group(1))

    typ = re.search(r'\b(DDR[2345])\b', memory, re.IGNORECASE)
    if typ:
        spec['type'] = typ.group(1).upper()

    ch = re.search(r'(\d+)\s*x\s*\d+\s*(?:GB|MB)', memory, re.IGNORECASE)
    if ch:
        spec['channels'] = int(ch.group(1))

    return {'_flat': {}, '_spec': spec}


# ---------------------------------------------------------------------------
# Sidecar JSON import
# ---------------------------------------------------------------------------

SIDECAR_PREFIX = 'hardware-spec-'
SIDECAR_SUFFIX = '.json'


def is_sidecar_filename(name: str) -> bool:
    fn = name.lower().strip()
    return fn.startswith(SIDECAR_PREFIX) and fn.endswith(SIDECAR_SUFFIX)


def system_id_from_sidecar_filename(name: str) -> str:
    fn = name.strip()
    if fn.lower().startswith(SIDECAR_PREFIX):
        fn = fn[len(SIDECAR_PREFIX):]
    if fn.lower().endswith(SIDECAR_SUFFIX):
        fn = fn[:-len(SIDECAR_SUFFIX)]
    return fn.strip()


# Maps sidecar dotted paths → (target_column, json_key_in_column)
# None = skip.  '' = top-level column.  Nested path = JSON key path inside that column.
_SIDECAR_MAP: dict[str, tuple[str, str | None]] = {
    # Flat columns
    'cpu.model':            ('cpu_model', None),
    'cpu.cores':            ('cpu_cores', None),
    'cpu.threads':          ('cpu_threads', None),
    'gpu.model':            ('gpu_model', None),

    # cpu_spec JSON
    'cpu.arch_family':      ('cpu_spec', 'arch_family'),
    'cpu.base_clock_mhz':   ('cpu_spec', 'base_clock_mhz'),
    'cpu.boost_clock_mhz':  ('cpu_spec', 'boost_clock_mhz'),
    'cpu.tdp_watts':        ('cpu_spec', 'tdp_watts'),
    'cpu.tdp_pl1_watts':    ('cpu_spec', 'tdp_pl1_watts'),
    'cpu.tdp_pl2_watts':    ('cpu_spec', 'tdp_pl2_watts'),
    'cpu.l3_cache_kb':      ('cpu_spec', 'l3_cache_kb'),
    'cpu.l2_cache_kb':      ('cpu_spec', 'l2_cache_kb'),
    'cpu.clusters':         ('cpu_spec', 'clusters'),

    # gpu_spec JSON
    'gpu.vram_mb':          ('gpu_spec', 'vram_mb'),
    'gpu.core_clock_mhz':   ('gpu_spec', 'core_clock_mhz'),
    'gpu.boost_clock_mhz':  ('gpu_spec', 'boost_clock_mhz'),
    'gpu.tdp_watts':        ('gpu_spec', 'tdp_watts'),
    'gpu.shader_count':     ('gpu_spec', 'shader_count'),
    'gpu.tensor_cores':     ('gpu_spec', 'tensor_cores'),

    # memory_spec JSON
    'memory.size_mb':       ('memory_spec', 'size_mb'),
    'memory.type':          ('memory_spec', 'type'),
    'memory.speed_mhz':     ('memory_spec', 'speed_mhz'),
    'memory.channels':      ('memory_spec', 'channels'),

    # storage_spec JSON (the whole array)
    'storage':              ('storage_spec', None),
}


def _deep_get(d: dict, key: str, default=None):
    parts = key.split('.')
    for p in parts:
        if isinstance(d, dict):
            d = d.get(p)
        else:
            return default
    return d if d is not None else default


_BLOB_PREFIX_MAP: dict[str, str] = {
    'cpu': 'cpu_spec', 'gpu': 'gpu_spec',
    'memory': 'memory_spec', 'storage': 'storage_spec',
}


def _infer_col(path: str) -> tuple[str, str] | None:
    """Map ``cpu.arch_family`` → ``(cpu_spec, arch_family)``, or None."""
    dot = path.find('.')
    if dot == -1:
        return None
    prefix, rest = path[:dot], path[dot + 1:]
    col = _BLOB_PREFIX_MAP.get(prefix)
    if col is None or not rest:
        return None
    return col, rest


def _walk_unknown(obj: Any, prefix: str = '') -> list[tuple[str, str, Any]]:
    """Collect (blob_col, field_name, value) for paths not in _SIDECAR_MAP."""
    collected: list[tuple[str, str, Any]] = []
    if not isinstance(obj, dict):
        return collected
    for k, v in obj.items():
        path = f'{prefix}.{k}' if prefix else k
        if path in _SIDECAR_MAP:
            continue  # already handled above
        inferred = _infer_col(path)
        if inferred:
            col, field = inferred
            collected.append((col, field, v))
        elif isinstance(v, dict):
            collected.extend(_walk_unknown(v, path))
    return collected


def apply_sidecar_to_spec(spec: HardwareSpec, data: dict) -> None:
    """Update a HardwareSpec from a nested sidecar JSON dict (in-place)."""
    # Known keys from _SIDECAR_MAP
    for sidecar_key, (col_name, json_key) in _SIDECAR_MAP.items():
        val = _deep_get(data, sidecar_key)
        if val is None:
            continue
        if json_key is None:
            setattr(spec, col_name, val)
        else:
            blob = _safe_get_json(spec, col_name)
            blob[json_key] = val
            setattr(spec, col_name, blob)

    # Unknown nested keys → inferred blob column
    for col, field, val in _walk_unknown(data):
        blob = _safe_get_json(spec, col)
        blob[field] = val
        setattr(spec, col, blob)


def _safe_get_json(spec: HardwareSpec, col: str) -> dict:
    v = getattr(spec, col, None)
    return v if isinstance(v, dict) else {}


# ---------------------------------------------------------------------------
# Auto-populate from system.hardware
# ---------------------------------------------------------------------------

def auto_populate_hardware_spec(system: System) -> HardwareSpec | None:
    """
    Create or update a HardwareSpec for *system* by parsing its PTS hardware string.
    Returns the spec (new or existing). Does NOT commit.
    """
    spec = HardwareSpec.query.filter_by(system_id=system.id).first()
    if spec is None:
        spec = HardwareSpec(system_id=system.id, source='auto')
        db.session.add(spec)

    hw = system.hardware or ''

    for parser, flat_pairs, json_col in [
        (parse_cpu_spec, [('cpu_model', 'cpu_model'), ('cpu_cores', 'cpu_cores'), ('cpu_threads', 'cpu_threads')], 'cpu_spec'),
        (parse_gpu_spec, [('gpu_model', 'gpu_model')], 'gpu_spec'),
        (parse_memory_spec, [], 'memory_spec'),
    ]:
        result = parser(hw)
        flat = result.get('_flat', {})
        jspec = result.get('_spec', {})
        if not flat and not jspec:
            continue
        # Set flat columns only when null
        for sidecar_key, col_name in flat_pairs:
            if getattr(spec, col_name, None) is None and flat.get(sidecar_key) is not None:
                setattr(spec, col_name, flat[sidecar_key])
        # Merge into JSON blob
        if jspec:
            blob = _safe_get_json(spec, json_col)
            changed = False
            for k, v in jspec.items():
                if k not in blob:
                    blob[k] = v
                    changed = True
            if changed:
                setattr(spec, json_col, blob)

    return spec


# ---------------------------------------------------------------------------
# Missing data hints — driven by SpecFieldSchema, not hardcoded
# ---------------------------------------------------------------------------


def missing_spec_hints(spec: HardwareSpec | None) -> list[dict[str, str]]:
    """
    Return hints for fields defined in SpecFieldSchema that are missing from *spec*.
    Category labels are derived from the blob_column name.
    """
    schemas = SpecFieldSchema.query.order_by(
        SpecFieldSchema.blob_column, SpecFieldSchema.sort_order,
    ).all()

    category_labels: dict[str, str] = {
        'cpu_spec': 'CPU', 'gpu_spec': 'GPU',
        'memory_spec': 'Memory', 'storage_spec': 'Storage',
    }

    hints: list[dict[str, str]] = []
    for s in schemas:
        missing = False
        if spec is None:
            missing = True
        else:
            blob = getattr(spec, s.blob_column, None)
            if not isinstance(blob, dict) or s.field_name not in blob or blob.get(s.field_name) is None:
                missing = True
        if missing:
            hints.append({
                'blob': s.blob_column,
                'field': s.field_name,
                'category': category_labels.get(s.blob_column, s.blob_column),
                'hint': s.hint or f'{s.label} is not set',
            })
    return hints


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------

def spec_get(spec: HardwareSpec | None, blob_col: str, field: str, default=None):
    """Safely extract a value from a spec's JSON blob."""
    if spec is None:
        return default
    blob = getattr(spec, blob_col, None)
    if not isinstance(blob, dict):
        return default
    return blob.get(field, default)
