"""Capture system profile at import time so distinct upload runs stay identifiable."""

from __future__ import annotations

from typing import Any

from app.system_util import hardware_fingerprint

PROFILE_SNAPSHOT_KEYS = (
    "primary_system_name",
    "serial_number",
    "chassis_version",
    "custom_hardware",
    "cooler_model",
    "psu_model",
    "psu_wattage",
    "external_off",
    "gpu_fans",
    "memory_fans",
    "nvme_fans",
    "manual_notes",
)


def capture_profile_snapshot(system) -> dict[str, Any]:
    """JSON-safe profile fields frozen at upload time."""
    snap: dict[str, Any] = {
        "hardware_fingerprint": hardware_fingerprint(system.hardware or ""),
    }
    for key in PROFILE_SNAPSHOT_KEYS:
        val = getattr(system, key, None)
        if isinstance(val, bool):
            snap[key] = val
        elif val is not None and str(val).strip():
            snap[key] = str(val).strip()
        else:
            snap[key] = val if isinstance(val, bool) else (str(val).strip() if val else "")
    return snap


def profile_fingerprint(snapshot: dict[str, Any] | None) -> str:
    """Stable key: same snapshot → same observation cohort."""
    if not snapshot:
        return "default"
    parts = [str(snapshot.get("hardware_fingerprint") or "")]
    for key in PROFILE_SNAPSHOT_KEYS:
        parts.append(f"{key}={snapshot.get(key)!r}")
    return "|".join(parts)


def format_observation_label(
    system,
    snapshot: dict[str, Any] | None,
    imported_at,
) -> str:
    """Short label for charts when one system has multiple upload runs."""
    base = (getattr(system, "identifier", None) or "").strip() or f"system-{system.id}"
    serial = (snapshot or {}).get("serial_number") or getattr(system, "serial_number", None) or ""
    serial = str(serial).strip()
    cooler = (snapshot or {}).get("cooler_model") or (snapshot or {}).get("custom_hardware") or ""
    date_s = ""
    if imported_at is not None:
        date_s = imported_at.strftime("%Y-%m-%d") if hasattr(imported_at, "strftime") else str(imported_at)[:10]
    if serial:
        suffix = f"SN {serial}"
        if cooler:
            suffix += f", {cooler}"
        if date_s:
            suffix += f", {date_s}"
        return f"{base} ({suffix})"
    if cooler and date_s:
        return f"{base} ({cooler}, {date_s})"
    if date_s:
        return f"{base} ({date_s})"
    if cooler:
        return f"{base} ({cooler})"
    return base
