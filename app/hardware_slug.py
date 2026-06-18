"""Compact hardware abbreviations for profile identifiers."""

from __future__ import annotations

import re

from app.components import (
    extract_hardware_component,
    normalize_graphics_name,
    normalize_processor_name,
)

_SLUG_SAFE_RE = re.compile(r'[^a-z0-9]+')


def _slug_token(text: str, *, max_len: int = 24) -> str:
    token = _SLUG_SAFE_RE.sub('', (text or '').lower())
    return token[:max_len] if token else ''


def _shorten_cpu_model(model: str) -> str:
    """13600K -> 136k, 290HX-Plus -> 290hxp."""
    model = (model or '').lower().replace('-', '')
    model = model.replace('plus', 'p').replace('max', 'm')

    # Strip groups of 2+ trailing zeros that pad the suffix (13600K -> 136k).
    # But keep a single trailing zero (9950X -> 9950x).
    m = re.match(r'(\d+?)(0{2,})([a-z]+)$', model)
    if m and len(m.group(1)) >= 3:
        model = m.group(1) + m.group(3)

    return model


def abbreviate_processor(processor: str) -> str:
    """
  Short CPU tag for identifiers.

  Examples:
    Intel Core i5-13600K -> ci5-136k
    Intel Core Ultra 9 290HX-Plus -> cu9-29hxp
    AMD Ryzen 9 9950X -> ar9-9950x
    """
    raw = normalize_processor_name(processor)
    if not raw:
        return ''

    s = raw.lower()

    m = re.search(r'core\s+ultra\s+(\d+)\s+([\w\-]+)', s)
    if m:
        return f'cu{m.group(1)}-{_shorten_cpu_model(m.group(2))}'

    m = re.search(r'core\s+i(\d+)[\s\-]+([\w\d\-]+)', s)
    if m:
        return f'ci{m.group(1)}-{_shorten_cpu_model(m.group(2))}'

    m = re.search(r'ryzen\s+(?:ai\s+)?(\d+)\s+([\w\d\-]+)', s)
    if m:
        return f'ar{m.group(1)}-{_shorten_cpu_model(m.group(2))}'

    return _slug_token(raw, max_len=16)


def abbreviate_memory(memory: str) -> str:
    """
  DIMM layout + speed tag.

  Examples:
    1 x 32GB DDR5 @ 5600 MT/s -> 1x32g56
    2 x 16GB DDR5-4800 -> 2x16g48
    """
    if not memory:
        return ''

    s = memory.lower()
    count = None
    size_per = None

    m = re.search(r'(\d+)\s*x\s*(\d+)\s*g', s)
    if m:
        count, size_per = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r'(\d+)\s*g(?:b)?(?:\s|$|,)', s)
        if m:
            count = 1
            size_per = int(m.group(1))

    speed = None
    for pattern in (
        r'ddr\d+-(\d{4})',
        r'@\s*(\d{4})\s*(?:mt/s|mhz)?',
        r'(\d{4})\s*mt/s',
        r'(\d{4})\s*mhz',
    ):
        m = re.search(pattern, s)
        if m:
            speed = int(m.group(1))
            break

    if count and size_per:
        slug = f'{count}x{size_per}g'
        if speed:
            slug += str(speed // 100)
        return slug

    return _slug_token(memory, max_len=12)


def abbreviate_disk(disk: str) -> str:
    """
    Short disk/storage abbreviation for profile identifiers.

    Examples:
        Samsung SSD 990 Pro 2TB -> s-990pro-2t
        Samsung SSD 980 Pro 1TB -> s-980pro-1t
        WD Blue SN580 2TB -> wd-bluesn580-2t
        Crucial T700 1TB -> c-t700-1t
    """
    if not disk:
        return ''

    s = disk.lower().strip()

    brand_map = {
        'samsung': 's',
        'western digital': 'wd',
        'wd': 'wd',
        'corsair': 'c',
        'crucial': 'c',
        'sk hynix': 'h',
        'hynix': 'h',
        'kingston': 'k',
        'seagate': 'st',
        'micron': 'm',
        'intel': 'i',
        'sabrent': 'sb',
        'teamgroup': 'tg',
        'adata': 'ad',
    }

    brand = ''
    for name in sorted(brand_map, key=len, reverse=True):
        if s.startswith(name):
            brand = brand_map[name]
            s = s[len(name):].strip()
            break

    for w in [
        'nvme', 'ssd', 'solid state', 'm.2', 'drive', 'internal',
        'pcie', 'gen5', 'gen4', 'gen3', 'plus', 'ultra',
    ]:
        s = s.replace(w, '')
    s = re.sub(r'\s+', ' ', s).strip()

    capacity = ''
    m = re.search(r'(\d+)\s*(tb|gb)', s)
    if m:
        cap_num = m.group(1)
        cap_unit = m.group(2)[0]
        capacity = f'{cap_num}{cap_unit}'
        s = (s[:m.start()] + s[m.end():]).strip()

    s = re.sub(r'\s+', '', s)
    s = _slug_token(s, max_len=10)

    parts = [p for p in [brand, s, capacity] if p]
    return '-'.join(parts)


def abbreviate_graphics(graphics: str) -> str:
    """RTX 4080 Laptop GPU -> rtx4080l, Radeon RX 7900 XTX -> rx7900xtx."""
    raw = normalize_graphics_name(graphics)
    if not raw:
        return ''

    s = raw.lower()
    # Preserve "laptop" so mobile and desktop SKUs stay distinct.
    laptop = 'l' if 'laptop' in s else ''
    s = s.replace('laptop', '').replace('geforce', '').replace('radeon', '')
    s = re.sub(r'\bnvidia\b', '', s)
    s = re.sub(r'\bamd\b', '', s)
    s = re.sub(r'\s+', '', s)

    m = re.search(r'((?:rtx|gtx|rx|arc)\s*\d+\w*)', s.replace(' ', ''))
    if m:
        token = m.group(1)
        if laptop and not token.endswith('l'):
            token += laptop
        return token

    return _slug_token(raw, max_len=14)


def build_hardware_slug(
    hardware: str,
    *,
    serial_number: str | None = None,
) -> str:
    """Join CPU, memory, GPU, and optional serial into one identifier suffix."""
    parts: list[str] = []

    processor = (
        extract_hardware_component(hardware, 'Processor')
        or extract_hardware_component(hardware, 'CPU')
        or ''
    )
    memory = (
        extract_hardware_component(hardware, 'Memory')
        or extract_hardware_component(hardware, 'RAM')
        or ''
    )
    graphics = (
        extract_hardware_component(hardware, 'Graphics')
        or extract_hardware_component(hardware, 'GPU')
        or ''
    )

    for abbrev in (
        abbreviate_processor(processor),
        abbreviate_memory(memory),
        abbreviate_graphics(graphics),
    ):
        if abbrev and abbrev not in parts:
            parts.append(abbrev)

    disk_str = extract_hardware_component(hardware, 'Disk')
    if disk_str:
        drives = [d.strip() for d in disk_str.split('+') if d.strip()]
        disk_abbrevs = [abbreviate_disk(d) for d in drives]
        disk_abbrevs = [a for a in disk_abbrevs if a and a not in parts]
        if disk_abbrevs:
            parts.append('+'.join(disk_abbrevs))

    serial = re.sub(r'[^a-z0-9]+', '', (serial_number or '').strip().lower())
    if serial:
        prefix = '' if serial.startswith('sn') else 'sn'
        sn_part = f'{prefix}{serial}'
        if len(sn_part) > 16:
            sn_part = sn_part[:16]
        parts.append(sn_part)

    return '-'.join(parts)


def profile_identifier(base_identifier: str, hardware_slug: str) -> str:
    """Combine XML/base name with a hardware slug."""
    base = (base_identifier or '').strip()
    slug = (hardware_slug or '').strip().lower()
    if not base:
        return slug or 'unknown-system'
    if not slug:
        return base
    return f'{base}__{slug}'
