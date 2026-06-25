from __future__ import annotations

import math
import secrets
import statistics
from collections import defaultdict

from app import db
from app.benchmark_util import delete_orphan_benchmarks
from app.components import get_system_components
from app.models import Benchmark, BenchmarkResult, System
from app.system_util import base_system_identifier, hardware_fingerprint


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


# Ordered list of (key, label) for "Compare by" dropdown; key must match get_system_components() keys.
COMPARE_BY_OPTIONS = [
    ('system_name', 'System name'),
    ('identifier', 'System identifier'),
    ('native_resolution', 'Native display resolution'),
    ('processor', 'CPU (Processor)'),
    ('graphics', 'GPU (Graphics)'),
    ('memory', 'Memory'),
    ('motherboard', 'Motherboard'),
    ('chipset', 'Chipset'),
    ('storage', 'Storage drives'),
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


def serialize_compare_system_groups(grouped_systems):
    """JSON-safe grouped systems for the compare page picker."""
    out = []
    for group in grouped_systems:
        out.append({
            'group_name': group['group_name'],
            'profiles': [
                {
                    'id': sys.id,
                    'identifier': sys.identifier,
                    'primary_group_name': sys.primary_group_name,
                    'profile_label': sys.profile_label,
                    'components': get_system_components(sys),
                }
                for sys in group['profiles']
            ],
        })
    return out


def _reconcile_primary_name_conflict(primary_name):
    """
    After a primary_system_name change, check if multiple systems share the same name.
    - If hardware + software match exactly: merge results into one, delete duplicates.
    - If hardware or software differ: rename identifiers with distinguishing suffixes.
    """
    from app.hardware_slug import build_hardware_slug, profile_identifier

    systems = System.query.filter(
        System.primary_system_name == primary_name
    ).all()

    if len(systems) < 2:
        return

    groups = defaultdict(list)
    for sys in systems:
        fp = (hardware_fingerprint(sys.hardware), hardware_fingerprint(sys.software))
        groups[fp].append(sys)

    for group in groups.values():
        if len(group) > 1:
            target = group[0]
            for source in group[1:]:
                BenchmarkResult.query.filter_by(system_id=source.id).update(
                    {"system_id": target.id},
                    synchronize_session=False,
                )
                if source.hardware_spec:
                    db.session.delete(source.hardware_spec)
                db.session.delete(source)
            delete_orphan_benchmarks()

    remaining = System.query.filter(
        System.primary_system_name == primary_name
    ).all()

    if len(remaining) == 1:
        sys = remaining[0]
        base = base_system_identifier(primary_name)
        if sys.identifier != base:
            sys.identifier = base
    else:
        for sys in remaining:
            base = base_system_identifier(primary_name)
            slug = build_hardware_slug(sys.hardware, serial_number=getattr(sys, 'serial_number', None))
            new_id = profile_identifier(base, slug)
            taken = {
                row[0] for row in db.session.query(System.identifier)
                .filter(
                    db.or_(
                        System.identifier == new_id,
                        System.identifier.like(f'{new_id}-%'),
                    )
                ).all()
            }
            n = 2
            while new_id in taken:
                new_id = f'{profile_identifier(base, slug)}-{n}'
                n += 1
            if new_id != sys.identifier:
                sys.identifier = new_id


def generate_comparison_id():
    return secrets.token_hex(8)
