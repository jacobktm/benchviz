"""Helper functions for workload characterization — perf detection, argument matching."""

from __future__ import annotations

import re
from typing import Any

from ..models import Benchmark
from ._constants import _COUNTER_ALIASES, _PERF_MARKERS


def _norm_text(*parts: str) -> str:
    return " ".join((p or "").strip().lower() for p in parts if p)


def is_perf_counter_benchmark(benchmark: Benchmark, arguments: str = "") -> bool:
    if benchmark.display_format != "BAR_GRAPH":
        return False
    if getattr(benchmark, "is_primary", True) is False:
        blob = _norm_text(benchmark.identifier, benchmark.title, benchmark.description, benchmark.scale, arguments)
        if any(m in blob for m in _PERF_MARKERS):
            return True
        scale_l = (benchmark.scale or "").strip().lower()
        if scale_l in {alias for alias, _ in _COUNTER_ALIASES}:
            return True
    blob = _norm_text(benchmark.identifier, benchmark.title, benchmark.description, benchmark.scale, arguments)
    return any(m in blob for m in _PERF_MARKERS)


def counter_signal_key(benchmark: Benchmark, arguments: str = "") -> str | None:
    blob = _norm_text(benchmark.scale, benchmark.description, benchmark.title, arguments)
    for needle, key in _COUNTER_ALIASES:
        if needle in blob:
            return key
    if "perf " in blob or "perf-" in blob:
        m = re.search(r"perf[\s\-/]+stat[\s\-/]+e[\s\-/]+([a-z0-9][a-z0-9\-/]*)", blob)
        if not m:
            m = re.search(r"perf[\s\-/]+([a-z0-9][a-z0-9\-/]*)", blob)
        if m:
            return m.group(1).replace("/", "_").replace("-", "_")
    return None


def _args_matches_config(result_args: str | None, config_args: str) -> bool:
    ra = (result_args or "").strip()
    ca = (config_args or "").strip()
    if ra == ca:
        return True
    if not ca:
        return not ra
    return ra.endswith(ca) or ca in ra


def _monitor_result_matches_config(result_args: str | None, config_args: str) -> bool:
    ra = (result_args or "").strip()
    ca = (config_args or "").strip()
    if ra == ca:
        return True
    if not ca:
        return True
    return ra.endswith(ca) or ca in ra


def option_profile_key(description: str | None, scale: str | None = None) -> str:
    desc = (description or "").strip() or "primary"
    sc = (scale or "").strip()
    return f"{desc}|{sc}" if sc else desc


def _result_option_suffix(result_args: str | None, config_args: str) -> str:
    ra = (result_args or "").strip()
    ca = (config_args or "").strip()
    if not ra:
        return ""
    if ca:
        if ra == ca:
            return ""
        if ra.endswith(ca):
            return ra[: -len(ca)].strip()
    return ra


def _result_matches_option(
    result_args: str | None,
    config_args: str,
    option_description: str = "",
    option_scale: str = "",
) -> bool:
    if not (option_description or option_scale):
        return True
    if not _args_matches_config(result_args, config_args):
        return False
    suffix = _result_option_suffix(result_args, config_args)
    if not suffix:
        return True
    blob = _norm_text(option_description, option_scale)
    suf = _norm_text(suffix)
    if not blob:
        return True
    if suf in blob or blob in suf:
        return True
    opt_tokens = {t for t in re.findall(r"[a-z0-9]+", blob) if len(t) >= 3}
    suf_tokens = {t for t in re.findall(r"[a-z0-9]+", suf) if len(t) >= 3}
    return bool(opt_tokens & suf_tokens)


def _sensor_label(benchmark: Benchmark) -> str:
    return _norm_text(benchmark.description, benchmark.metric if hasattr(benchmark, "metric") else "", benchmark.scale)
