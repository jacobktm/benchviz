"""Phoronix Test Suite comparison hashing and test profile identifiers."""

from __future__ import annotations

import hashlib
import re


def proportion_is_lower_better(proportion: str | None) -> bool:
    p = (proportion or "").strip().upper()
    if p == "LIB":
        return True
    if p == "HIB":
        return False
    pl = (proportion or "").lower()
    if "lower" in pl and "better" in pl:
        return True
    if "higher" in pl and "better" in pl:
        return False
    if "more" in pl and "better" in pl:
        return False
    if "lower" in pl:
        return True
    return not ("higher" in pl or "more" in pl)


def _is_hib(proportion: str | None) -> bool:
    return not proportion_is_lower_better(proportion)


COMPOSITE_OPTION_CAP_RATIO = 1.5


def strip_test_profile_identifier(identifier: str | None) -> str:
    tp = (identifier or "").strip().replace("\\", "/")
    if not tp:
        return ""
    m = re.search(r"-(\d+)\.(\d+)\.(\d+)$", tp)
    if m:
        tp = tp[: m.start()] + f"-{m.group(1)}.{m.group(2)}"
    return tp


def hash_identifier_from_test_profile(test_profile: str | None) -> str:
    return strip_test_profile_identifier(test_profile)


def test_profile_family(name: str | None) -> str:
    s = (name or "").strip().replace("\\", "/")
    if not s:
        return ""
    return re.sub(r"-\d[\d.]*$", "", s)


def normalize_ob_unit(unit: str | None) -> str:
    u = (unit or "").strip()
    if not u:
        return ""
    ul = u.lower()
    if ul in ("seconds", "second", "sec", "s"):
        return "seconds"
    if ul in ("ms", "millisecond", "milliseconds"):
        return "ms"
    if ul in ("mb/s", "mib/s"):
        return "mb/s"
    if ul in ("gb/s", "gib/s"):
        return "gb/s"
    if ul in ("fps", "frames per second", "frame/s"):
        return "fps"
    if ul in ("mips",):
        return "mips"
    if ul in ("iops",):
        return "iops"
    return ul


def parse_version_tuple(version: str | None) -> tuple[int, ...]:
    version = (version or "").strip()
    if not version:
        return ()
    parts: list[int] = []
    for piece in re.split(r"[._-]", version):
        if piece.isdigit():
            parts.append(int(piece))
        elif piece:
            break
    return tuple(parts)


def generate_comparison_hash(
    test_identifier: str,
    arguments: str = "",
    attributes: str = "",
    version: str = "",
    result_scale: str = "",
    *,
    hex_digest: bool = True,
) -> str:
    parts = [
        test_identifier or "",
        (arguments or "").strip(),
        (attributes or "").strip(),
        (version or "").strip(),
        (result_scale or "").strip(),
    ]
    payload = ",".join(parts)
    digest = hashlib.sha1(payload.encode("utf-8")).digest()
    return digest.hex() if hex_digest else digest


def comparison_hash_for_benchmark(
    *,
    identifier: str | None,
    title: str | None,
    description: str | None,
    app_version: str | None,
    scale: str | None,
    arguments: str = "",
) -> str:
    tp = strip_test_profile_identifier(identifier)
    if not tp:
        tp = (title or "").strip()
    return generate_comparison_hash(
        tp,
        arguments or "",
        description or "",
        app_version or "",
        scale or "",
    )
