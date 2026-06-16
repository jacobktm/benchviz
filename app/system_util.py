"""System identity helpers for benchmark import."""

import re

from . import db
from .hardware_slug import build_hardware_slug, profile_identifier
from .models import System

_LEGACY_SUFFIX_RE = re.compile(r'^(.+) \((\d+)\)$')
_HARDWARE_SUFFIX_RE = re.compile(r'^(.+)__(.+)$')


def hardware_fingerprint(hardware: str) -> str:
    """Normalized hardware string for same-machine detection."""
    text = (hardware or '').strip().lower()
    return re.sub(r'\s+', ' ', text)


def normalize_serial_number(serial: str | None) -> str:
    """Case-insensitive serial match key (whitespace stripped)."""
    return re.sub(r'\s+', '', (serial or '').strip().lower())


def base_system_identifier(identifier: str) -> str:
    """
    Canonical grouping key for a system identifier.

    Legacy imports used ``name (2)`` suffixes; hardware-distinguished profiles
    use ``name__cpu-mem-gpu`` — strip both so rows group on the dashboard.
    """
    ident = (identifier or '').strip() or 'unknown-system'
    m = _LEGACY_SUFFIX_RE.match(ident)
    if m:
        return m.group(1)
    m = _HARDWARE_SUFFIX_RE.match(ident)
    if m:
        return m.group(1)
    return ident


def _disambiguated_identifier(base_id: str) -> str:
    """Next free identifier when the XML has no identifier: base, base (2), …"""
    base_id = (base_id or '').strip()
    if not base_id:
        base_id = 'unknown-system'

    taken = {
        row[0]
        for row in db.session.query(System.identifier).filter(
            db.or_(
                System.identifier == base_id,
                System.identifier.like(f'{base_id} (%)'),
                System.identifier.like(f'{base_id}__%'),
            )
        ).all()
    }

    if base_id not in taken:
        return base_id

    n = 2
    while f'{base_id} ({n})' in taken:
        n += 1
    return f'{base_id} ({n})'


def _candidate_systems(identifier: str) -> list[System]:
    """All profile rows for an XML/base identifier."""
    base = base_system_identifier(identifier)
    if not base:
        return []
    return System.query.filter(
        db.or_(
            System.identifier == base,
            System.identifier.like(f'{base} (%)'),
            System.identifier.like(f'{base}__%'),
            System.primary_system_name == base,
        )
    ).all()


def _unique_profile_identifier(base_id: str, hardware_slug: str) -> str:
    """Pick a free storage identifier for a new profile variant."""
    candidate = profile_identifier(base_id, hardware_slug)
    taken = {
        row[0]
        for row in db.session.query(System.identifier).filter(
            db.or_(
                System.identifier == candidate,
                System.identifier.like(f'{candidate}-%'),
            )
        ).all()
    }
    if candidate not in taken:
        return candidate

    n = 2
    while f'{candidate}-{n}' in taken:
        n += 1
    return f'{candidate}-{n}'


def _profile_matches(system: System, hw_fp: str, serial_number: str | None) -> bool:
    """Hardware must match; when a serial is set on either side, both must agree."""
    if hardware_fingerprint(system.hardware) != hw_fp:
        return False
    want = normalize_serial_number(serial_number)
    have = normalize_serial_number(getattr(system, 'serial_number', None) or '')
    if want and have:
        return want == have
    if want or have:
        return False
    return True


def resolve_system_for_import(
    identifier: str,
    hardware: str,
    software: str,
    user: str,
    timestamp: str,
    *,
    fallback_hardware: str | None = None,
    serial_number: str | None = None,
) -> tuple[System, bool, str | None]:
    """
    Find an existing system record or create one.

    Same base identifier + same hardware fingerprint → reuse (update metadata).
    When serial numbers are provided, they must also match.
    Additional profiles under the same XML identifier get a storage identifier
    augmented with compact hardware tags (``qa-lemp13__ci5-136k-1x32g56``).

    Returns (system, created_new, note_for_log).
    """
    identifier = (identifier or '').strip()
    serial_number = (serial_number or '').strip() or None
    hw_fp = hardware_fingerprint(hardware or fallback_hardware)
    base_id = base_system_identifier(identifier) if identifier else ''

    for system in _candidate_systems(identifier):
        if not _profile_matches(system, hw_fp, serial_number):
            continue
        system.hardware = hardware or system.hardware or ''
        system.software = software or system.software or ''
        system.user = user or system.user or ''
        system.timestamp = timestamp or system.timestamp or ''
        if serial_number:
            system.serial_number = serial_number
        return system, False, None

    exact = System.query.filter_by(identifier=identifier).first() if identifier else None
    if exact and hw_fp and not hardware_fingerprint(exact.hardware):
        if _profile_matches(exact, hw_fp, serial_number):
            exact.hardware = hardware or exact.hardware or ''
            exact.software = software or exact.software or ''
            exact.user = user or exact.user or ''
            exact.timestamp = timestamp or exact.timestamp or ''
            if serial_number:
                exact.serial_number = serial_number
            return exact, False, None

    candidates = _candidate_systems(identifier) if identifier else []
    if identifier and candidates:
        slug = build_hardware_slug(hardware or fallback_hardware or '', serial_number=serial_number)
        storage_id = _unique_profile_identifier(base_id, slug)
    else:
        storage_id = identifier or _disambiguated_identifier('unknown-system')

    created_new = True
    note = None
    if identifier and storage_id != identifier:
        note = (
            f'Added profile "{storage_id}" under family "{base_id}" '
            f'(hardware-distinguished identifier).'
        )
    elif identifier and serial_number and any(
        hardware_fingerprint(s.hardware) == hw_fp for s in candidates
    ):
        note = (
            f'Added profile for serial {serial_number!r} under identifier "{identifier}".'
        )
    elif identifier and exact and hw_fp and hardware_fingerprint(exact.hardware) != hw_fp:
        note = (
            f'Hardware differs from an existing "{identifier}" profile; '
            f'added another hardware profile under the same identifier.'
        )
    elif not identifier and storage_id != identifier:
        note = f'Created system as "{storage_id}".'

    system = System(
        identifier=storage_id,
        hardware=hardware or '',
        software=software or '',
        user=user or '',
        timestamp=timestamp or '',
        primary_system_name=base_id or storage_id,
        serial_number=serial_number,
    )
    db.session.add(system)
    db.session.flush()
    return system, created_new, note
