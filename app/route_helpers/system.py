from __future__ import annotations

from app import db
from app.components import (
    clean_text,
    extract_hardware_component,
    get_primary_group_name,
    get_system_components,
)
from app.models import System, SystemNvmeConfig
from app.parser import BOOL_PROFILE_FIELDS, STRING_PROFILE_FIELDS
from app.system_util import base_system_identifier


def get_unique_field_values():
    unique_values = {}
    for field in STRING_PROFILE_FIELDS:
        if field == 'manual_notes':
            continue
        column = getattr(System, field)
        values = db.session.query(column).distinct().filter(column.isnot(None), column != '').order_by(column).all()
        unique_values[field] = sorted(list(set(v[0].strip() for v in values if v[0] and v[0].strip())))
    return unique_values


def checkbox_value(form, key):
    return form.get(key) in {'on', 'true', '1', 'yes'}


def build_system_profile_from_form(form):
    profile = {field: clean_text(form.get(field)) for field in STRING_PROFILE_FIELDS}
    for field in BOOL_PROFILE_FIELDS:
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


def get_profile_badges(system):
    badges = []
    serial = clean_text(getattr(system, 'serial_number', None))
    if serial:
        badges.append(f'SN {serial}')
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
    comps = get_system_components(system)

    for key in [
        'processor', 'graphics', 'memory', 'storage', 'motherboard', 'chipset',
        'chassis_version', 'cooler_model', 'psu', 'custom_hardware',
    ]:
        val = comps.get(key, '')
        if val and not any(val in b for b in badges):
            badges.append(val)

    if not badges:
        return base_name
    return f"{base_name} | {' | '.join(badges)}"


def get_system_search_tags(system):
    tags = {
        clean_text(system.identifier).lower(),
        clean_text(getattr(system, 'serial_number', None)).lower(),
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


def group_system_profiles(systems_raw):
    """Group profile rows under primary system name for dashboard and compare pickers."""
    grouped_systems_dict = {}
    for sys in systems_raw:
        display_name = get_primary_group_name(sys) or sys.identifier or 'unknown-system'
        group_key = base_system_identifier(display_name).lower()
        sys.primary_group_name = display_name
        sys.search_tags = get_system_search_tags(sys)

        if group_key not in grouped_systems_dict:
            grouped_systems_dict[group_key] = {
                'group_name': display_name,
                'profiles': [],
                'search_tags': set(),
            }

        group = grouped_systems_dict[group_key]
        group['profiles'].append(sys)
        group['search_tags'].update(sys.search_tags)

    for group in grouped_systems_dict.values():
        profiles = group['profiles']
        group_name = group['group_name']

        if len(profiles) > 1:
            comps = [get_system_components(p) for p in profiles]
            all_keys = set()
            for c in comps:
                all_keys.update(c.keys())
            varying = set()
            for key in all_keys:
                if len({c.get(key, '') for c in comps}) > 1:
                    varying.add(key)
            hw_order = ['processor', 'graphics', 'memory', 'storage', 'motherboard', 'chipset']
            sw_order = ['os', 'kernel_version', 'nvidia_driver', 'mesa_version']
            order = hw_order + [k for k in sw_order if k in varying]
            for p, c in zip(profiles, comps):
                parts = []
                for key in order:
                    if key in varying:
                        val = c.get(key, '')
                        if val:
                            parts.append(val)
                for badge_key in [
                    'chassis_version', 'cooler_model', 'psu', 'custom_hardware',
                    'nvme_fans', 'thermal_pad_above_nvme',
                    'thermal_pad_below_nvme', 'thermal_pad_sandwich_nvme',
                ]:
                    if badge_key in varying:
                        val = c.get(badge_key, '')
                        if val:
                            parts.append(val)
                if parts:
                    p.profile_label = f"{group_name} | {' | '.join(parts)}"
                else:
                    p.profile_label = group_name
        else:
            profiles[0].profile_label = group_name

        profiles.sort(key=lambda s: (s.identifier or '').lower())
        group['search_tags_str'] = ' '.join(group['search_tags'])

    return sorted(grouped_systems_dict.values(), key=lambda g: g['group_name'].lower())
