"""
Workload characterization from LINUX_PERF counters and PTS MONITOR sensors.

Used to:
  - Route Performance Insights hardware features by measured bottleneck class
  - Filter compare-page sensors to those plausibly tied to the workload
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from typing import Any

from . import db
from .models import Benchmark, BenchmarkResult
from .sensor_quality import is_noisy_sensor_series, peak_series_value, sensor_kind
from .workload_consensus import (
    MIN_ACTIVE_SHARE,
    MIN_SYSTEMS_WITH_SENSOR_EVIDENCE,
    active_bottlenecks_from_scores,
    average_score_dicts,
    classification_from_cohort_consensus,
    scope_consensus,
    score_proportions,
    signals_have_evidence,
)


# Maps insight scope -> component keys (mirrors app_main INSIGHT_*_SCOPED_KEYS).
SCOPE_HARDWARE_KEYS: dict[str, frozenset[str]] = {
    "cpu": frozenset({
        "processor", "memory", "motherboard", "chipset", "os", "kernel_version",
        "llvm_version", "cooler_model", "chassis_version", "psu", "custom_hardware",
        "external_off", "memory_fans",
    }),
    "gpu": frozenset({
        "graphics", "nvidia_driver", "mesa_version", "llvm_version", "vulkan_driver",
        "processor", "memory", "os", "chassis_version", "gpu_fans",
    }),
    "storage": frozenset({
        "nvme_fans", "thermal_pad_above_nvme", "thermal_pad_below_nvme",
        "thermal_pad_sandwich_nvme", "custom_hardware", "chassis_version", "processor",
    }),
    "memory": frozenset({
        "processor", "memory", "motherboard", "chipset", "cooler_model", "memory_fans",
        "chassis_version", "psu",
    }),
    "general": frozenset({
        "processor", "graphics", "memory", "motherboard", "chipset", "os",
        "kernel_version", "nvidia_driver", "mesa_version", "llvm_version",
        "vulkan_driver", "chassis_version", "cooler_model", "psu", "custom_hardware",
        "external_off", "gpu_fans", "memory_fans", "nvme_fans",
    }),
}

SCOPE_SENSOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cpu": (
        "cpu temp", "cpu temperature", "cpu freq", "cpu frequency", "cpu usage",
        "cpu power", "cpu util", "energy-cores", "energy-pkg", "package power",
    ),
    "gpu": (
        "gpu temp", "gpu temperature", "gpu freq", "gpu frequency", "gpu usage",
        "gpu power", "gpu util", "energy-gpu", "graphics",
    ),
    "storage": ("nvme", "disk", "ssd", "storage", "read", "write i/o"),
    "memory": ("memory", "ram", "swap", "dimm"),
    "general": (
        "cpu temp", "cpu temperature", "cpu freq", "cpu frequency", "cpu usage",
        "cpu power", "gpu temp", "gpu temperature", "gpu freq", "gpu frequency",
        "gpu usage", "gpu power", "memory", "ram", "nvme", "energy",
    ),
}

# Substrings that identify a BAR_GRAPH row as a Linux perf counter.
_PERF_MARKERS = ("perf ", "perf-", "perf/")

_COUNTER_ALIASES: list[tuple[str, str]] = [
    ("instructions", "instructions"),
    ("cycles", "cycles"),
    ("cache-references", "cache_references"),
    ("cache-misses", "cache_misses"),
    ("branch-instructions", "branch_instructions"),
    ("branch-misses", "branch_misses"),
    ("context-switches", "context_switches"),
    ("cpu-migrations", "cpu_migrations"),
    ("page-faults", "page_faults"),
    ("energy-pkg", "energy_pkg"),
    ("energy-cores", "energy_cores"),
    ("energy-gpu", "energy_gpu"),
]


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
    """
    Match MONITOR / perf time-series rows to a primary BAR_GRAPH config.

    PTS prefixes sensor arguments (e.g. ``CPU Usage (Summary) <option> -P``) while the
    primary score row often uses just ``<option>`` or an empty default config.
    """
    ra = (result_args or "").strip()
    ca = (config_args or "").strip()
    if ra == ca:
        return True
    if not ca:
        # Default/empty config — accept any MONITOR row for this title/version/system.
        return True
    return ra.endswith(ca) or ca in ra


def option_profile_key(description: str | None, scale: str | None = None) -> str:
    """Stable key for a primary BAR_GRAPH option within a config."""
    desc = (description or "").strip() or "primary"
    sc = (scale or "").strip()
    return f"{desc}|{sc}" if sc else desc


def _result_option_suffix(result_args: str | None, config_args: str) -> str:
    """Prefix on result.arguments before the config suffix (option-specific sensor/perf runs)."""
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
    """
    True when a perf/sensor result belongs to this primary option.

    Results that only match the config (no option prefix) are shared across options.
    """
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


def _sensor_category(label: str) -> str | None:
    l = label.lower()
    if "gpu" in l:
        return "gpu"
    if "cpu" in l:
        return "cpu"
    if any(k in l for k in ("nvme", "disk", "ssd", "storage")):
        return "storage"
    if any(k in l for k in ("memory", "ram", "dimm")):
        return "memory"
    return None


def collect_workload_signals(
    title: str,
    app_version: str,
    config_args: str = "",
    system_ids: list[int] | None = None,
    *,
    option_description: str = "",
    option_scale: str = "",
) -> dict[str, Any]:
    """
    Median perf-counter values and monitor sensor means for one benchmark config/option.
    """
    config_args_db = "" if (not config_args or config_args == "default") else config_args

    perf_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(False),
    )
    if app_version:
        perf_q = perf_q.filter(Benchmark.app_version == app_version)
    perf_bms = [b for b in perf_q.all() if is_perf_counter_benchmark(b)]

    perf_values: dict[str, list[float]] = defaultdict(list)
    for bm in perf_bms:
        key = counter_signal_key(bm)
        if not key:
            continue
        res_q = BenchmarkResult.query.filter_by(benchmark_id=bm.id)
        if system_ids:
            res_q = res_q.filter(BenchmarkResult.system_id.in_(system_ids))
        for res in res_q.all():
            if not _result_matches_option(
                res.arguments, config_args_db, option_description, option_scale,
            ):
                continue
            if res.value is None:
                continue
            try:
                perf_values[key].append(float(res.value))
            except (TypeError, ValueError):
                pass

    perf_signals = {
        k: statistics.median(vs)
        for k, vs in perf_values.items()
        if vs
    }

    sensor_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "LINE_GRAPH",
    )
    if app_version:
        sensor_q = sensor_q.filter(Benchmark.app_version == app_version)
    sensor_keywords = (
        "temperature", "frequency", "usage", "power", "celsius", "mhz", "watts",
        "fan", "rpm", "voltage", "energy", "utilization",
    )
    sensors = [
        s for s in sensor_q.all()
        if s.description and any(k in s.description.lower() for k in sensor_keywords)
    ]

    sensor_signals: dict[str, float] = {}
    sensor_by_cat: dict[str, list[float]] = defaultdict(list)
    for s_bm in sensors:
        label = _sensor_label(s_bm)
        cat = _sensor_category(label)
        res_q = BenchmarkResult.query.filter_by(benchmark_id=s_bm.id)
        if system_ids:
            res_q = res_q.filter(BenchmarkResult.system_id.in_(system_ids))
        vals: list[float] = []
        for res in res_q.all():
            if not _result_matches_option(
                res.arguments, config_args_db, option_description, option_scale,
            ):
                continue
            if not res.data_json:
                continue
            if is_noisy_sensor_series(res.data_json, s_bm.description, s_bm.scale):
                continue
            kind = sensor_kind(s_bm.description, s_bm.scale)
            if kind in ("usage", "frequency"):
                peak = peak_series_value(res.data_json)
                if peak is not None:
                    vals.append(peak)
            else:
                nums = [float(x) for x in res.data_json if isinstance(x, (int, float))]
                if nums:
                    vals.append(statistics.mean(nums))
        if not vals:
            continue
        med = statistics.median(vals)
        sensor_signals[label] = med
        if cat:
            sensor_by_cat[cat].append(med)

    sensor_category_means = {
        cat: statistics.median(vs) for cat, vs in sensor_by_cat.items() if vs
    }

    return {
        "perf": perf_signals,
        "sensors": sensor_signals,
        "sensor_categories": sensor_category_means,
    }


def collect_workload_signals_by_system(
    title: str,
    app_version: str,
    config_args: str = "",
    system_ids: list[int] | None = None,
    *,
    option_description: str = "",
    option_scale: str = "",
) -> dict[int, dict[str, Any]]:
    """Per-system perf + sensor category signals for one benchmark config/option."""
    config_args_db = "" if (not config_args or config_args == "default") else config_args

    perf_by_sys: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    perf_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "BAR_GRAPH",
        Benchmark.is_primary.is_(False),
    )
    if app_version:
        perf_q = perf_q.filter(Benchmark.app_version == app_version)
    for bm in [b for b in perf_q.all() if is_perf_counter_benchmark(b)]:
        key = counter_signal_key(bm)
        if not key:
            continue
        res_q = BenchmarkResult.query.filter_by(benchmark_id=bm.id)
        if system_ids:
            res_q = res_q.filter(BenchmarkResult.system_id.in_(system_ids))
        for res in res_q.all():
            if not _result_matches_option(
                res.arguments, config_args_db, option_description, option_scale,
            ):
                continue
            if res.value is None:
                continue
            try:
                perf_by_sys[res.system_id][key].append(float(res.value))
            except (TypeError, ValueError):
                pass

    sensor_cat_by_sys: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    sensor_q = Benchmark.query.filter(
        Benchmark.title == title,
        Benchmark.display_format == "LINE_GRAPH",
    )
    if app_version:
        sensor_q = sensor_q.filter(Benchmark.app_version == app_version)
    sensor_keywords = (
        "temperature", "frequency", "usage", "power", "celsius", "mhz", "watts",
        "fan", "rpm", "voltage", "energy", "utilization",
    )
    sensors = [
        s for s in sensor_q.all()
        if s.description and any(k in s.description.lower() for k in sensor_keywords)
    ]
    for s_bm in sensors:
        cat = _sensor_category(_sensor_label(s_bm))
        if not cat:
            continue
        res_q = BenchmarkResult.query.filter_by(benchmark_id=s_bm.id)
        if system_ids:
            res_q = res_q.filter(BenchmarkResult.system_id.in_(system_ids))
        for res in res_q.all():
            if not _result_matches_option(
                res.arguments, config_args_db, option_description, option_scale,
            ):
                continue
            if not res.data_json:
                continue
            if is_noisy_sensor_series(res.data_json, s_bm.description, s_bm.scale):
                continue
            kind = sensor_kind(s_bm.description, s_bm.scale)
            if kind in ("usage", "frequency"):
                val = peak_series_value(res.data_json)
            else:
                nums = [float(x) for x in res.data_json if isinstance(x, (int, float))]
                val = statistics.mean(nums) if nums else None
            if val is not None:
                sensor_cat_by_sys[res.system_id][cat].append(float(val))

    all_ids = set(perf_by_sys.keys()) | set(sensor_cat_by_sys.keys())
    if system_ids:
        all_ids |= set(system_ids)

    out: dict[int, dict[str, Any]] = {}
    for sid in all_ids:
        perf_signals = {
            k: statistics.median(vs)
            for k, vs in perf_by_sys.get(sid, {}).items()
            if vs
        }
        sensor_category_means = {
            c: statistics.median(vs)
            for c, vs in sensor_cat_by_sys.get(sid, {}).items()
            if vs
        }
        out[sid] = {
            "perf": perf_signals,
            "sensor_categories": sensor_category_means,
        }
    return out


def _pool_signals(by_system: dict[int, dict[str, Any]], system_ids: list[int]) -> dict[str, Any]:
    """Median pool across systems (legacy aggregate path)."""
    perf_pooled: dict[str, list[float]] = defaultdict(list)
    cat_pooled: dict[str, list[float]] = defaultdict(list)
    for sid in system_ids:
        sig = by_system.get(sid) or {}
        for k, v in (sig.get("perf") or {}).items():
            perf_pooled[k].append(v)
        for k, v in (sig.get("sensor_categories") or {}).items():
            cat_pooled[k].append(v)
    return {
        "perf": {k: statistics.median(vs) for k, vs in perf_pooled.items() if vs},
        "sensors": {},
        "sensor_categories": {k: statistics.median(vs) for k, vs in cat_pooled.items() if vs},
    }


def _profile_extras(
    scope: str,
    active_bottlenecks: list[str] | None = None,
) -> dict[str, Any]:
    if active_bottlenecks and len(active_bottlenecks) >= 2:
        sensor_keywords: list[str] = []
        seen_kw: set[str] = set()
        hw_keys: set[str] = set()
        for bottleneck in active_bottlenecks:
            for kw in SCOPE_SENSOR_KEYWORDS.get(bottleneck, ()):
                if kw not in seen_kw:
                    sensor_keywords.append(kw)
                    seen_kw.add(kw)
            hw_keys |= SCOPE_HARDWARE_KEYS.get(bottleneck, frozenset())
        if "cpu" in active_bottlenecks or "gpu" in active_bottlenecks:
            for extra in ("cpu temp", "cpu power"):
                if extra not in seen_kw:
                    sensor_keywords.append(extra)
        return {
            "relevant_hardware_keys": sorted(hw_keys),
            "sensor_keywords": sensor_keywords,
            "active_bottlenecks": active_bottlenecks,
        }

    sensor_keywords = list(SCOPE_SENSOR_KEYWORDS.get(scope, SCOPE_SENSOR_KEYWORDS["general"]))
    if scope == "memory" and "cpu temp" not in sensor_keywords:
        sensor_keywords = list(sensor_keywords) + ["cpu temp", "cpu power"]
    if scope == "gpu":
        sensor_keywords = list(sensor_keywords) + ["cpu temp"]
    active = active_bottlenecks or ([scope] if scope in SCOPE_HARDWARE_KEYS and scope != "general" else [])
    return {
        "relevant_hardware_keys": sorted(SCOPE_HARDWARE_KEYS.get(scope, SCOPE_HARDWARE_KEYS["general"])),
        "sensor_keywords": sensor_keywords,
        "active_bottlenecks": active,
    }


def classify_workload(
    signals: dict[str, Any],
    title_blob: str = "",
) -> dict[str, Any]:
    """
    Infer bottleneck class from normalized perf + monitor signals.
    Falls back to title heuristics when counters are sparse.
    """
    perf = signals.get("perf") or {}
    cats = signals.get("sensor_categories") or {}

    scores = {"cpu": 0.0, "memory": 0.0, "gpu": 0.0, "storage": 0.0}
    evidence: list[str] = []

    instr = perf.get("instructions")
    cycles = perf.get("cycles")
    if instr and cycles and cycles > 0:
        ipc = instr / cycles
        if ipc >= 1.2:
            scores["cpu"] += 1.5
            evidence.append(f"IPC≈{ipc:.2f}")
        elif ipc < 0.8:
            scores["memory"] += 1.5
            evidence.append(f"low IPC≈{ipc:.2f}")

    refs = perf.get("cache_references")
    misses = perf.get("cache_misses")
    if refs and refs > 0 and misses is not None:
        miss_rate = misses / refs
        if miss_rate >= 0.04:
            scores["memory"] += 2.0 + min(2.0, miss_rate * 10)
            evidence.append(f"cache miss≈{miss_rate * 100:.1f}%")

    branches = perf.get("branch_instructions")
    branch_miss = perf.get("branch_misses")
    if branches and branches > 0 and branch_miss is not None:
        br_miss = branch_miss / branches
        if br_miss >= 0.02:
            scores["cpu"] += 1.0
            evidence.append(f"branch miss≈{br_miss * 100:.1f}%")

    gpu_usage = cats.get("gpu", 0)
    cpu_usage = cats.get("cpu", 0)
    if gpu_usage >= 40:
        scores["gpu"] += 3.0
        evidence.append(f"GPU usage≈{gpu_usage:.0f}")
    elif gpu_usage >= 15:
        scores["gpu"] += 1.5
    if cpu_usage >= 70 and gpu_usage < 20:
        scores["cpu"] += 1.5
        evidence.append(f"CPU usage≈{cpu_usage:.0f}")

    if perf.get("energy_gpu", 0) > perf.get("energy_cores", 0) * 0.3:
        scores["gpu"] += 1.0

    blob = (title_blob or "").lower()
    if any(k in blob for k in ("nvme", "fio", "disk", "storage", "ssd", "i/o")):
        scores["storage"] += 2.0
    if any(k in blob for k in ("vulkan", "cuda", "opengl", "blender", "gpu")):
        scores["gpu"] += 1.0
    if any(k in blob for k in ("kernel", "compile", "compression", "encode")):
        scores["cpu"] += 0.5

    if max(scores.values()) < 1.0:
        scope = _title_scope_fallback(blob)
        return {
            "scope": scope,
            "taxonomy": "mixed",
            "confidence": 0.25,
            "scores": scores,
            "score_proportions": score_proportions(scores),
            "active_bottlenecks": [scope] if scope in SCOPE_HARDWARE_KEYS and scope != "general" else [],
            "evidence": evidence,
            "source": "title_heuristic",
        }

    active = active_bottlenecks_from_scores(scores)
    props = score_proportions(scores)
    top = max(props.values())
    second = sorted(props.values(), reverse=True)[1] if len(props) > 1 else 0
    if len(active) >= 2:
        scope = "mixed"
        taxonomy = "mixed_workload"
        confidence = min(
            0.95,
            0.4 + 0.12 * max(scores.values()) + (0.08 if second >= top * 0.45 else 0),
        )
        evidence.append(f"multi-bottleneck: {', '.join(f'{k}≈{props[k] * 100:.0f}%' for k in active)}")
    else:
        scope = active[0] if active else max(scores, key=scores.get)
        taxonomy = {
            "cpu": "cpu_bound",
            "memory": "memory_bound",
            "gpu": "gpu_bound",
            "storage": "storage_bound",
        }.get(scope, "mixed")
        confidence = min(0.95, 0.35 + 0.15 * scores[scope] + (0.1 if top > second * 1.5 else 0))

    return {
        "scope": scope,
        "taxonomy": taxonomy,
        "active_bottlenecks": active,
        "confidence": round(confidence, 3),
        "scores": {k: round(v, 3) for k, v in scores.items()},
        "score_proportions": {k: round(props[k], 3) for k in props},
        "evidence": evidence,
        "source": "perf+sensors" if perf else "sensors+title",
    }


def _title_scope_fallback(blob: str) -> str:
    if any(k in blob for k in ("vulkan", "cuda", "opengl", "render", "graphics", "gpu ")):
        return "gpu"
    if any(k in blob for k in ("nvme", "disk", "io", "storage", "ssd", "fio")):
        return "storage"
    if "kernel" in blob and ("build" in blob or "compil" in blob):
        return "cpu"
    return "general"


def build_workload_profile(
    title: str,
    app_version: str,
    config_args: str = "",
    system_ids: list[int] | None = None,
    description: str = "",
    *,
    option_description: str = "",
    option_scale: str = "",
) -> dict[str, Any]:
    """
    Workload profile for a benchmark config and optional primary subtest (option).

    When some systems lack perf/MONITOR data but enough others have clean evidence,
    infer the same bottleneck mix (scope + score proportions) for the full cohort.
    """
    opt_desc = (option_description or description or "").strip()
    opt_scale = (option_scale or "").strip()
    title_blob = _norm_text(title, opt_desc, opt_scale, description, config_args)
    config_key = config_args or "default"
    option_key = option_profile_key(opt_desc, opt_scale) if (opt_desc or opt_scale) else ""

    if not system_ids:
        signals = collect_workload_signals(
            title, app_version, config_args, None,
            option_description=opt_desc, option_scale=opt_scale,
        )
        classification = classify_workload(signals, title_blob)
        return {
            **classification,
            "title": title,
            "app_version": app_version or "",
            "config_args": config_key,
            "option_key": option_key,
            "option_description": opt_desc,
            "option_scale": opt_scale,
            "signals": {
                "perf": signals.get("perf") or {},
                "sensor_categories": signals.get("sensor_categories") or {},
            },
            "imputation": {
                "applied": False,
                "reason": "no_system_list",
                "n_with_evidence": 0,
                "n_imputed": 0,
                "n_total": 0,
            },
            **_profile_extras(
                classification["scope"],
                classification.get("active_bottlenecks"),
            ),
        }

    by_system = collect_workload_signals_by_system(
        title, app_version, config_args, system_ids,
        option_description=opt_desc, option_scale=opt_scale,
    )
    evidenced_ids = [sid for sid in system_ids if signals_have_evidence(by_system.get(sid))]
    missing_ids = [sid for sid in system_ids if sid not in evidenced_ids]
    n_ev = len(evidenced_ids)
    n_total = len(system_ids)

    per_system_class: dict[str, dict[str, Any]] = {}
    for sid in system_ids:
        sig = by_system.get(sid) or {"perf": {}, "sensor_categories": {}}
        per_system_class[str(sid)] = classify_workload(sig, title_blob)

    imputation: dict[str, Any] = {
        "applied": False,
        "n_with_evidence": n_ev,
        "n_imputed": len(missing_ids),
        "n_total": n_total,
        "evidenced_system_ids": evidenced_ids,
        "imputed_system_ids": missing_ids,
    }

    if n_ev < MIN_SYSTEMS_WITH_SENSOR_EVIDENCE or not missing_ids:
        imputation["reason"] = (
            "insufficient_evidence" if n_ev < MIN_SYSTEMS_WITH_SENSOR_EVIDENCE
            else "all_have_data"
        )
        signals = _pool_signals(by_system, system_ids)
        classification = classify_workload(signals, title_blob)
    else:
        evidenced_scopes = [per_system_class[str(s)]["scope"] for s in evidenced_ids]
        avg_scores = average_score_dicts(
            [per_system_class[str(s)]["scores"] for s in evidenced_ids]
        )
        classification = classification_from_cohort_consensus(
            avg_scores,
            evidenced_scopes,
            n_ev,
            len(missing_ids),
            title_blob,
        )
        _, vote_dominant, vote_agreement = scope_consensus(evidenced_scopes)
        imputation["applied"] = True
        imputation["reason"] = (
            "cohort_multi_bottleneck"
            if len(classification.get("active_bottlenecks") or []) >= 2
            else "cohort_consensus"
        )
        imputation["scope_vote_dominant"] = vote_dominant
        imputation["scope_vote_agreement"] = round(vote_agreement, 3)
        imputation["score_proportions"] = classification.get("score_proportions")
        imputation["active_bottlenecks"] = classification.get("active_bottlenecks")
        signals = _pool_signals(by_system, evidenced_ids)
        for sid in missing_ids:
            per_system_class[str(sid)] = {
                **classification,
                "source": "imputed_from_cohort",
            }

    scope = classification["scope"]
    return {
        **classification,
        "title": title,
        "app_version": app_version or "",
        "config_args": config_key,
        "option_key": option_key,
        "option_description": opt_desc,
        "option_scale": opt_scale,
        "signals": {
            "perf": signals.get("perf") or {},
            "sensor_categories": signals.get("sensor_categories") or {},
        },
        "imputation": imputation,
        "per_system": per_system_class,
        **_profile_extras(scope, classification.get("active_bottlenecks")),
    }


def sensor_is_relevant(
    description: str | None,
    metric: str | None,
    workload: dict[str, Any] | None,
    *,
    strict: bool = True,
) -> bool:
    """True if a MONITOR sensor chart should be shown for this workload."""
    if not strict or not workload:
        return True
    keywords = workload.get("sensor_keywords") or []
    if not keywords:
        return True
    blob = _norm_text(description, metric)
    if not blob.strip():
        return True
    return any(k in blob for k in keywords)


def workload_scope_for_insights(
    title: str,
    app_version: str,
    config_args: str,
    analysis_json: dict | None,
    text_blob: str,
) -> str:
    """Prefer stored workload profile, else title heuristics."""
    return workload_context_for_insights(
        title, app_version, config_args, analysis_json, text_blob,
    )["scope"]


# Map ML bottleneck dimensions to hardware cohort scopes (cache/thermal → CPU-class parts).
_BOTTLENECK_HARDWARE_SCOPE: dict[str, str] = {
    "cache": "cpu",
    "thermal": "cpu",
}


def _normalize_insights_bottlenecks(active: list[str] | None) -> list[str]:
    """Collapse cache/thermal into CPU-class hardware for cohort filtering."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in active or []:
        mapped = _BOTTLENECK_HARDWARE_SCOPE.get(str(raw), str(raw))
        if mapped not in seen:
            seen.add(mapped)
            out.append(mapped)
    return out


def _ml_workload_context_from_analysis(
    analysis_json: dict,
    config_args: str,
) -> dict[str, Any] | None:
    """Insights routing from stored ML workload profile (_ml_profile)."""
    ml_root = analysis_json.get("_ml_profile")
    if not isinstance(ml_root, dict):
        return None
    key = "default" if (not config_args or config_args == "default") else config_args
    by_args = ml_root.get("by_args") or {}
    prof = by_args.get(key)
    if not isinstance(prof, dict) and len(by_args) == 1:
        prof = next(iter(by_args.values()))
    if not isinstance(prof, dict):
        return None
    wl = prof.get("workload")
    if not isinstance(wl, dict) or wl.get("insufficient_signal"):
        return None
    scope = str(wl.get("scope") or "general")
    props = wl.get("proportions") or {}
    active = _normalize_insights_bottlenecks(list(wl.get("active_bottlenecks") or []))
    if not active and scope not in ("general", "mixed") and scope:
        active = _normalize_insights_bottlenecks([scope])
    if scope == "mixed" and not active:
        active = _normalize_insights_bottlenecks([
            k for k, v in props.items()
            if k in SCOPE_HARDWARE_KEYS and v and float(v) >= MIN_ACTIVE_SHARE
        ])
    return {
        "scope": scope,
        "active_bottlenecks": active,
        "score_proportions": props,
        "source": "ml_profile",
        "confidence": wl.get("confidence"),
        "evidence": list(wl.get("evidence") or []),
    }


def workload_context_for_insights(
    title: str,
    app_version: str,
    config_args: str,
    analysis_json: dict | None,
    text_blob: str,
    *,
    option_description: str = "",
    option_scale: str = "",
) -> dict[str, Any]:
    """Scope plus active bottlenecks for insights routing."""
    fallback_scope = _title_scope_fallback(text_blob)
    if analysis_json:
        key = "default" if (not config_args or config_args == "default") else config_args
        ml_ctx = _ml_workload_context_from_analysis(analysis_json, key)
        if ml_ctx:
            return ml_ctx
        ok = option_profile_key(option_description, option_scale) if (option_description or option_scale) else ""
        by_option = analysis_json.get("_workload_by_option") or {}
        if ok and isinstance(by_option.get(key), dict) and by_option[key].get(ok):
            wl = by_option[key][ok]
        else:
            by_args = analysis_json.get("_workload_by_args") or {}
            wl = by_args.get(key) or analysis_json.get("_workload")
        if isinstance(wl, dict) and wl.get("scope"):
            return {
                "scope": str(wl["scope"]),
                "active_bottlenecks": _normalize_insights_bottlenecks(
                    list(wl.get("active_bottlenecks") or []),
                ),
                "score_proportions": wl.get("score_proportions") or {},
                "source": "legacy_profile",
            }
    return {
        "scope": fallback_scope,
        "active_bottlenecks": (
            [fallback_scope]
            if fallback_scope in SCOPE_HARDWARE_KEYS and fallback_scope != "general"
            else []
        ),
        "score_proportions": {},
        "source": "title_heuristic",
    }
