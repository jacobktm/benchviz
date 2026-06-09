"""System identity helpers for benchmark import."""

import re

from . import db
from .models import System


def hardware_fingerprint(hardware: str) -> str:
    """Normalized hardware string for same-machine detection."""
    text = (hardware or '').strip().lower()
    return re.sub(r'\s+', ' ', text)


def _disambiguated_identifier(base_id: str) -> str:
    """Next free identifier: base, or base (2), base (3), …"""
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
    Same identifier + different hardware → new record with (2), (3), … suffix.

    Returns (system, created_new, note_for_log).
    """
    identifier = (identifier or '').strip()
    hw_fp = hardware_fingerprint(hardware or fallback_hardware)

    if identifier:
        candidates = System.query.filter(
            db.or_(
                System.identifier == identifier,
                System.identifier.like(f'{identifier} (%)'),
            )
        ).all()
    else:
        candidates = []

    for system in candidates:
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

    new_identifier = _disambiguated_identifier(identifier or 'unknown-system')
    created_new = True
    note = None
    if exact and hw_fp and hardware_fingerprint(exact.hardware) != hw_fp:
        note = (
            f'Hardware differs from existing "{identifier}"; '
            f'created new system "{new_identifier}".'
        )
    elif new_identifier != identifier:
        note = f'Created system as "{new_identifier}".'

    system = System(
        identifier=new_identifier,
        hardware=hardware or '',
        software=software or '',
        user=user or '',
        timestamp=timestamp or '',
        primary_system_name=new_identifier,
    )
    db.session.add(system)
    db.session.flush()
    return system, created_new, note
