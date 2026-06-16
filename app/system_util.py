"""System identity helpers for benchmark import."""

import re

from . import db
from .models import System

_LEGACY_SUFFIX_RE = re.compile(r'^(.+) \((\d+)\)$')


def hardware_fingerprint(hardware: str) -> str:
    """Normalized hardware string for same-machine detection."""
    text = (hardware or '').strip().lower()
    return re.sub(r'\s+', ' ', text)


def base_system_identifier(identifier: str) -> str:
    """
    Canonical grouping key for a system identifier.

    Legacy imports used ``name (2)`` suffixes; strip those so older rows still
    group with the base identifier on the dashboard.
    """
    ident = (identifier or '').strip() or 'unknown-system'
    m = _LEGACY_SUFFIX_RE.match(ident)
    return m.group(1) if m else ident


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
    """All profile rows for an XML identifier (includes legacy suffixed imports)."""
    identifier = (identifier or '').strip()
    if not identifier:
        return []
    return System.query.filter(
        db.or_(
            System.identifier == identifier,
            System.identifier.like(f'{identifier} (%)'),
        )
    ).all()


def resolve_system_for_import(
    identifier: str,
    hardware: str,
    software: str,
    user: str,
    timestamp: str,
    *,
    fallback_hardware: str | None = None,
) -> tuple[System, bool, str | None]:
    """
    Find an existing system record or create one.

    Same identifier + same hardware fingerprint → reuse (update metadata).
    Same identifier + different hardware → additional profile row with the
    **same** identifier (grouped on the dashboard under that name).

    Returns (system, created_new, note_for_log).
    """
    identifier = (identifier or '').strip()
    hw_fp = hardware_fingerprint(hardware or fallback_hardware)

    for system in _candidate_systems(identifier):
        if hardware_fingerprint(system.hardware) == hw_fp:
            system.hardware = hardware or system.hardware or ''
            system.software = software or system.software or ''
            system.user = user or system.user or ''
            system.timestamp = timestamp or system.timestamp or ''
            return system, False, None

    exact = System.query.filter_by(identifier=identifier).first() if identifier else None
    if exact and hw_fp and not hardware_fingerprint(exact.hardware):
        # Existing row with empty hardware — treat first upload with hardware as the same machine.
        exact.hardware = hardware or exact.hardware or ''
        exact.software = software or exact.software or ''
        exact.user = user or exact.user or ''
        exact.timestamp = timestamp or exact.timestamp or ''
        return exact, False, None

    storage_id = identifier or _disambiguated_identifier('unknown-system')
    created_new = True
    note = None
    if identifier and exact and hw_fp and hardware_fingerprint(exact.hardware) != hw_fp:
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
        primary_system_name=identifier or storage_id,
    )
    db.session.add(system)
    db.session.flush()
    return system, created_new, note
