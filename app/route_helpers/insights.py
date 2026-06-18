from __future__ import annotations

import statistics
from collections import defaultdict

from app import db
from app.components import get_system_components
from app.models import BenchmarkAnalysis, BenchmarkResult
from app.pts import proportion_is_lower_better
from app.repositories import BenchmarkRepository, SystemRepository
from app.route_helpers.compare import COMPARE_BY_OPTIONS


# Hint substrings for CPU-bound workloads (used to scope leaderboard features).
_CPU_BENCHMARK_HINTS = (
    "stockfish", "chess",
    "7-zip", "7zip",
    "compression", "decompression",
    "openssl",
    "ffmpeg", "x264", "x265", "handbrake", "encoding", "transcod",
    "coremark", "pybench", "phpbench",
    "compilebench",
    "dav1d", "rav1e", "svt-av1",
    "blosc", "lz4", "zstd",
    "redis", "memcached",
    "sqlite",
)

# NVMe chassis layout toggles: often aligned with system type and replicate across
# machines; they are misleading on CPU/GPU workloads unless the test is storage-class.
NVME_LAYOUT_LEADERBOARD_KEYS = frozenset({
    "thermal_pad_above_nvme",
    "thermal_pad_below_nvme",
    "thermal_pad_sandwich_nvme",
    "nvme_fans",
})

INSIGHT_CPU_SCOPED_KEYS = frozenset({
    "processor",
    "memory",
    "motherboard",
    "chipset",
    "os",
    "kernel_version",
    "llvm_version",
    "cooler_model",
    "chassis_version",
    "psu",
    "custom_hardware",
    "external_off",
    "memory_fans",
})
INSIGHT_GPU_SCOPED_KEYS = frozenset({
    "graphics",
    "nvidia_driver",
    "mesa_version",
    "llvm_version",
    "vulkan_driver",
    "processor",
    "memory",
    "os",
    "chassis_version",
    "gpu_fans",
})
INSIGHT_STORAGE_SCOPED_KEYS = frozenset({
    "nvme_fans",
    "thermal_pad_above_nvme",
    "thermal_pad_below_nvme",
    "thermal_pad_sandwich_nvme",
    "custom_hardware",
    "chassis_version",
    "external_off",
    "psu",
    "memory",
    "processor",
})


def _insights_infer_scope(text_blob: str) -> str:
    scope = "general"
    if ("kernel" in text_blob and ("build" in text_blob or "compil" in text_blob or "make" in text_blob
         or "gcc" in text_blob or "clang" in text_blob)) or ("compil" in text_blob and "linux" in text_blob):
        scope = "cpu"
    elif any(h in text_blob for h in _CPU_BENCHMARK_HINTS):
        scope = "cpu"
    elif any(k in text_blob for k in ["vulkan", "cuda", "opengl", "render", "graphics", "gpu "]):
        scope = "gpu"
    elif any(k in text_blob for k in ["nvme", "disk", "io", "i/o", "storage", "ssd", "hdd", "throughput",
                                        "fio", "postmark", "iometer"]):
        scope = "storage"
    return scope


def _insights_workload_context_from_analysis(
    title: str, app_version: str, args_str: str, text_blob: str,
) -> dict:
    """Prefer perf/sensor workload profile from BenchmarkAnalysis when available."""
    from app.workload_profile import workload_context_for_insights

    records = BenchmarkAnalysis.query.filter_by(
        benchmark_title=title,
        benchmark_app_version=app_version or "",
    ).all()
    analysis_json = records[0].analysis_json if records else None
    args_key = "default" if (not args_str or args_str.lower() == "default") else args_str
    return workload_context_for_insights(title, app_version, args_key, analysis_json, text_blob)


def _insights_allowed_singles_for_scope(
    scope: str,
    include_all_component_keys: bool,
    active_bottlenecks: list[str] | None = None,
):
    from app.analyzer import INSIGHT_COMPONENT_KEYS
    from app.workload_profile import SCOPE_HARDWARE_KEYS, _normalize_insights_bottlenecks

    normalized = _normalize_insights_bottlenecks(active_bottlenecks)
    if normalized and len(normalized) >= 2:
        allowed = set()
        for bottleneck in normalized:
            allowed |= SCOPE_HARDWARE_KEYS.get(bottleneck, frozenset())
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k in allowed]
    elif normalized and len(normalized) == 1:
        bn = normalized[0]
        if bn in SCOPE_HARDWARE_KEYS and bn != "general":
            allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k in SCOPE_HARDWARE_KEYS[bn]]
        else:
            allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k not in NVME_LAYOUT_LEADERBOARD_KEYS]
    elif scope == "mixed" and normalized:
        allowed = set()
        for bottleneck in normalized:
            allowed |= SCOPE_HARDWARE_KEYS.get(bottleneck, frozenset())
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k in allowed]
    elif scope in SCOPE_HARDWARE_KEYS and scope != "general":
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k in SCOPE_HARDWARE_KEYS[scope]]
    elif include_all_component_keys:
        allowed_singles = list(INSIGHT_COMPONENT_KEYS)
    else:
        allowed_singles = [k for k in INSIGHT_COMPONENT_KEYS if k not in NVME_LAYOUT_LEADERBOARD_KEYS]
    if not allowed_singles:
        allowed_singles = list(INSIGHT_COMPONENT_KEYS)
    return allowed_singles


def _load_primary_insights_bundle(title, app_version, args_str, scope_override=""):
    """
    Load primary BAR_GRAPH scores and component maps for a benchmark/config.
    Returns (bundle_dict, None) or (None, (error_message, http_code)).
    """
    from app.analyzer import MIN_SYSTEMS_TOTAL

    title = (title or "").strip()
    app_version = (app_version or "").strip()
    args_str = (args_str or "").strip()
    scope_override = (scope_override or "").strip().lower()

    if not title:
        return None, ("Missing benchmark_title query parameter", 400)

    primary_bms = BenchmarkRepository.find_primary_by_title(title, app_version)
    if not primary_bms:
        return None, ("No primary BAR_GRAPH benchmark found for the given title/app_version", 404)

    rep_bm = primary_bms[0]
    label_map = dict(COMPARE_BY_OPTIONS)

    is_lower_better = any(proportion_is_lower_better(b.proportion) for b in primary_bms)
    y_flip = -1.0 if is_lower_better else 1.0

    args_analysis_key = "default" if (not args_str or args_str.lower() == "default") else args_str
    args_db = "" if args_analysis_key == "default" else args_str

    primary_bm_ids = [b.id for b in primary_bms]
    all_results = BenchmarkResult.query.filter(
        BenchmarkResult.benchmark_id.in_(primary_bm_ids),
        BenchmarkResult.arguments == args_db,
        BenchmarkResult.value.isnot(None),
    ).all()

    if not all_results:
        return None, ("No BAR_GRAPH results for this benchmark/config", 404)

    by_system_vals = defaultdict(list)
    for r in all_results:
        by_system_vals[r.system_id].append(r.value)

    y_norm_by_system = {}
    y_raw_by_system = {}
    for sid, vals in by_system_vals.items():
        y_raw = statistics.mean([v for v in vals if v is not None])
        y_raw_by_system[sid] = y_raw
        y_norm_by_system[sid] = y_raw * y_flip

    sys_ids = sorted(y_raw_by_system.keys())
    systems = SystemRepository.find_by_ids(sys_ids)
    systems_by_id = {s.id: s for s in systems}
    comps_by_sid = {s.id: get_system_components(s) for s in systems}

    text_blob = " ".join([
        (rep_bm.title or ""),
        (rep_bm.description or ""),
        args_str or "",
    ]).lower()
    wl_ctx = _insights_workload_context_from_analysis(title, app_version, args_analysis_key, text_blob)
    scope = wl_ctx["scope"]
    active_bottlenecks = list(wl_ctx.get("active_bottlenecks") or [])
    if scope == "general" and not active_bottlenecks:
        inferred = _insights_infer_scope(text_blob)
        if inferred != "general":
            scope = inferred
            active_bottlenecks = [inferred]
    include_all_component_keys = scope_override == "all"
    if scope_override in {"all", "general"}:
        scope = "general"
        active_bottlenecks = []
    elif scope_override in {"cpu", "gpu", "storage", "memory"}:
        scope = scope_override
        active_bottlenecks = [scope_override]
    allowed_singles = _insights_allowed_singles_for_scope(
        scope, include_all_component_keys, active_bottlenecks or None,
    )

    bundle = {
        "MIN_SYSTEMS_TOTAL": MIN_SYSTEMS_TOTAL,
        "rep_bm": rep_bm,
        "primary_bms": primary_bms,
        "primary_bm_ids": primary_bm_ids,
        "label_map": label_map,
        "y_label_base": rep_bm.scale or "Score",
        "is_lower_better": is_lower_better,
        "y_flip": y_flip,
        "args_analysis_key": args_analysis_key,
        "args_db": args_db,
        "y_raw_by_system": y_raw_by_system,
        "y_norm_by_system": y_norm_by_system,
        "sys_ids": sys_ids,
        "systems_by_id": systems_by_id,
        "comps_by_sid": comps_by_sid,
        "scope": scope,
        "allowed_singles": allowed_singles,
        "workload_context": wl_ctx,
        "title": title,
        "app_version": app_version,
    }
    return bundle, None


def _insights_signal_to_noise_raw(buckets_sid, y_raw_by_system):
    """
    Ratio: spread of cohort means / median within-cohort stdev (raw benchmark units).
    High values => cohort centroids differ more than typical scatter inside a cohort.
    """
    cohort_means = []
    inner_stds = []
    for sids in buckets_sid.values():
        ys = [y_raw_by_system[sid] for sid in sids if sid in y_raw_by_system]
        if not ys:
            continue
        cohort_means.append(statistics.mean(ys))
        if len(ys) > 1:
            inner_stds.append(statistics.stdev(ys))
    if len(cohort_means) < 2:
        return 0.0, 0.0
    spread = max(cohort_means) - min(cohort_means)
    med_inner = statistics.median(inner_stds) if inner_stds else 0.0
    sn = spread / (med_inner + 1e-9)
    return float(sn), float(spread)


def _insights_alignment_tier(eta_sq, sn_ratio):
    """
    Heuristic label for whether scores track this component split vs looking noise-like.
    Not causal — association only, with replication gates applied upstream.
    """
    eta_sq = float(eta_sq)
    sn_ratio = float(sn_ratio)
    if eta_sq >= 0.55 or sn_ratio >= 5.0:
        return (
            "strong",
            "Scores line up distinctly across these component values versus within-cohort scatter.",
        )
    if eta_sq >= 0.28 or sn_ratio >= 2.5:
        return (
            "moderate",
            "Meaningful-looking spread between cohorts; more data would firm up how much this part matters.",
        )
    return (
        "weak",
        "Alignment is limited: cohort differences are small relative to noise, or effects overlap a lot.",
    )


def _insights_alignment_rank_score(eta_sq, sn_ratio):
    """Order components by combined association strength (unitless, ~0–1)."""
    sn_term = min(1.0, float(sn_ratio) / 6.0)
    return 0.55 * float(eta_sq) + 0.45 * sn_term


def _insights_eta_squared_norm_buckets(value_to_y_norm_lists):
    vals = []
    for ys in value_to_y_norm_lists.values():
        vals.extend(ys)
    if len(vals) < 2:
        return 0.0
    grand_mean = statistics.mean(vals)
    ss_total = sum((y - grand_mean) ** 2 for y in vals)
    if ss_total < 1e-18:
        return 0.0
    ss_between = 0.0
    for ys in value_to_y_norm_lists.values():
        nj = len(ys)
        mj = statistics.mean(ys)
        ss_between += nj * (mj - grand_mean) ** 2
    return ss_between / ss_total
