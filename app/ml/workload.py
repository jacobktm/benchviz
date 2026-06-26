"""
Workload fingerprint: CPU / GPU / cache / memory / storage / thermal dominance.

Uses perf counters and MONITOR sensors (usage and thermal kept separate).
CPU/GPU utilization is compared against per-model idle/load ranges learned from
all benchmarks in the fleet; idle GPU/CPU readings actively suppress that component.
"""

from __future__ import annotations

from typing import Any

from app.workload_consensus import MIN_ACTIVE_SHARE, active_bottlenecks_from_scores, score_proportions

# Raw % fallbacks when hardware baselines are not yet available.
_IDLE_GPU_RAW_PCT = 8.0
_IDLE_CPU_RAW_PCT = 5.0
_IDLE_LOAD_FRAC = 0.12
# Proxy load (thermal/power/freq) is slightly discounted vs direct utilization.
_PROXY_LOAD_WEIGHT = 0.88

# Normalized proxy signals and weights when usage % is unavailable.
_PROXY_SIGNALS: dict[str, list[tuple[str, str, float]]] = {
    "cpu": [
        ("power", "cpu_power_load_frac", 0.45),
        ("temp", "cpu_temp_load_frac", 0.35),
        ("freq", "cpu_freq_load_frac", 0.12),
        ("droop", "cpu_freq_droop_frac", 0.08),
    ],
    "gpu": [
        ("power", "gpu_power_load_frac", 0.55),
        ("temp", "gpu_temp_load_frac", 0.45),
    ],
}


_WORKLOAD_KEYS = ("cpu", "gpu", "cache", "memory", "storage")


def _title_blob(title: str, description: str = "") -> str:
    return f"{title} {description}".lower()


def _title_score_fallback(blob: str) -> dict[str, float]:
    scores = {"cpu": 0.0, "gpu": 0.0, "cache": 0.0, "memory": 0.0, "storage": 0.0, "thermal": 0.0}
    if any(k in blob for k in ("vulkan", "cuda", "opengl", "render", "graphics", "gpu ")):
        scores["gpu"] += 2.0
    if any(k in blob for k in ("nvme", "disk", "io", "storage", "ssd", "fio")):
        scores["storage"] += 2.0
    if any(k in blob for k in ("compile", "compression", "encode", "kernel", "openssl", "crack", "crypto")):
        scores["cpu"] += 1.0
    if any(k in blob for k in ("stream", "memory", "ram bandwidth")):
        scores["memory"] += 2.0
    if any(k in blob for k in ("av1", "aom", "video", "ffmpeg")):
        scores["cpu"] += 1.2
    return scores


def _resolve_load_level(
    sensor_pool: dict[str, float | None],
    peak_key: str,
    frac_key: str,
    *,
    idle_raw_pct: float,
) -> float | None:
    """Map a sensor to 0–1 load vs fleet idle/load span; None when no reading exists."""
    frac = sensor_pool.get(frac_key)
    if frac is not None:
        return max(0.0, float(frac))
    raw = sensor_pool.get(peak_key)
    if raw is None:
        return None
    raw_f = float(raw)
    if raw_f < idle_raw_pct:
        return 0.0
    return min(1.0, raw_f / 100.0)


def _apply_utilization_scores(
    scores: dict[str, float],
    evidence: list[str],
    sensor_pool: dict[str, float | None],
) -> bool:
    """
    Drive CPU/GPU scores from actual utilization vs fleet/model idle-load ranges.
    Returns True when at least one usage channel was observed (even if idle).
    """
    cpu_load = _resolve_load_level(
        sensor_pool, "cpu_usage_peak", "cpu_usage_load_frac", idle_raw_pct=_IDLE_CPU_RAW_PCT,
    )
    gpu_load = _resolve_load_level(
        sensor_pool, "gpu_usage_peak", "gpu_usage_load_frac", idle_raw_pct=_IDLE_GPU_RAW_PCT,
    )
    has_cpu = bool(sensor_pool.get("has_cpu_usage") or cpu_load is not None)
    has_gpu = bool(sensor_pool.get("has_gpu_usage") or gpu_load is not None)
    if not has_cpu and not has_gpu:
        return False

    cpu_load = cpu_load if cpu_load is not None else 0.0
    gpu_load = gpu_load if gpu_load is not None else 0.0

    if has_gpu and gpu_load < _IDLE_LOAD_FRAC:
        gpu_peak = sensor_pool.get("gpu_usage_peak")
        if gpu_peak is not None:
            evidence.append(f"GPU idle ({gpu_peak:.0f}% peak vs fleet baseline)")
        elif sensor_pool.get("gpu_usage_load_frac") is not None:
            evidence.append(f"GPU idle ({sensor_pool['gpu_usage_load_frac'] * 100:.0f}% of model load span)")
        gpu_load = 0.0

    if has_cpu and cpu_load >= _IDLE_LOAD_FRAC:
        scores["cpu"] += 2.0 + cpu_load * 2.5
        if sensor_pool.get("cpu_usage_load_frac") is not None:
            evidence.append(f"CPU load≈{cpu_load * 100:.0f}% of model span")
        else:
            evidence.append(f"CPU usage peak≈{sensor_pool.get('cpu_usage_peak', 0):.0f}%")

    if has_gpu and gpu_load >= _IDLE_LOAD_FRAC:
        scores["gpu"] += 2.0 + gpu_load * 2.5
        if sensor_pool.get("gpu_usage_load_frac") is not None:
            evidence.append(f"GPU load≈{gpu_load * 100:.0f}% of model span")
        else:
            evidence.append(f"GPU usage peak≈{sensor_pool.get('gpu_usage_peak', 0):.0f}%")

    if has_cpu and cpu_load >= 0.20 and (not has_gpu or gpu_load == 0.0):
        scores["cpu"] += 1.5
        if "CPU-only" not in " ".join(evidence):
            evidence.append("CPU-only utilization (GPU near idle)")

    return True


def _proxy_load_for(
    component: str,
    sensor_pool: dict[str, float | None],
) -> tuple[float | None, str | None]:
    """
    Estimate 0–1 load from power/temp/freq vs fleet baselines when usage % is missing.
    Only uses hardware-normalized fractions or correlated secondary signals (slope/droop).
    """
    weighted = 0.0
    total_w = 0.0
    sources: list[str] = []
    for name, key, weight in _PROXY_SIGNALS.get(component, []):
        val = sensor_pool.get(key)
        if val is not None:
            weighted += float(val) * weight
            total_w += weight
            sources.append(name)
    if total_w > 0:
        return weighted / total_w, "+".join(sources)

    if component != "cpu":
        return None, None

    # Weak raw fallbacks: rising temp / freq droop correlate with CPU load.
    load_est = 0.0
    parts: list[str] = []
    slope_frac = sensor_pool.get("cpu_temp_slope_frac")
    if slope_frac is not None and float(slope_frac) >= 0.25:
        load_est = max(load_est, float(slope_frac))
        parts.append("temp slope")
    else:
        slope = sensor_pool.get("cpu_temp_slope")
        if slope is not None and float(slope) > 0.12:
            load_est = max(load_est, min(0.45, float(slope) * 1.5))
            parts.append("temp slope")

    droop_frac = sensor_pool.get("cpu_freq_droop_frac")
    if droop_frac is not None and float(droop_frac) >= 0.20:
        load_est = max(load_est, float(droop_frac))
        parts.append("freq droop")
    else:
        droop = sensor_pool.get("cpu_freq_droop")
        if droop is not None and float(droop) >= 150:
            load_est = max(load_est, min(0.55, float(droop) / 500.0))
            parts.append("freq droop")

    if parts and load_est > 0:
        return load_est, "+".join(parts)
    return None, None


def _component_has_proxy_data(component: str, sensor_pool: dict[str, float | None]) -> bool:
    if component == "cpu":
        keys = (
            "cpu_power_load_frac", "cpu_temp_load_frac", "cpu_freq_load_frac",
            "cpu_freq_droop_frac", "cpu_temp_slope_frac", "cpu_freq_droop", "cpu_temp_slope",
        )
        flags = ("has_cpu_temp", "has_cpu_power")
    else:
        keys = ("gpu_power_load_frac", "gpu_temp_load_frac", "gpu_freq_load_frac")
        flags = ("has_gpu_temp", "has_gpu_power")
    return any(sensor_pool.get(k) is not None for k in keys) or any(sensor_pool.get(f) for f in flags)


def _apply_proxy_load_scores(
    scores: dict[str, float],
    evidence: list[str],
    sensor_pool: dict[str, float | None],
    *,
    cpu_usage_seen: bool,
    gpu_usage_seen: bool,
) -> bool:
    """
    Infer CPU/GPU load from thermal/power/freq when utilization % is unavailable.
    """
    observed = False
    cpu_load = gpu_load = 0.0
    cpu_inferred = gpu_inferred = False

    if not cpu_usage_seen and _component_has_proxy_data("cpu", sensor_pool):
        proxy, src = _proxy_load_for("cpu", sensor_pool)
        if proxy is not None:
            observed = True
            cpu_load = proxy
            cpu_inferred = True
            if proxy >= _IDLE_LOAD_FRAC:
                scores["cpu"] += (1.5 + proxy * 2.0) * _PROXY_LOAD_WEIGHT
                evidence.append(f"CPU load inferred from {src}≈{proxy * 100:.0f}% of model span")
            else:
                evidence.append(f"CPU near idle ({src} proxy)")

    if not gpu_usage_seen and _component_has_proxy_data("gpu", sensor_pool):
        proxy, src = _proxy_load_for("gpu", sensor_pool)
        if proxy is not None:
            observed = True
            gpu_load = proxy
            gpu_inferred = True
            if proxy >= _IDLE_LOAD_FRAC:
                scores["gpu"] += (1.5 + proxy * 2.0) * _PROXY_LOAD_WEIGHT
                evidence.append(f"GPU load inferred from {src}≈{proxy * 100:.0f}% of model span")
            elif sensor_pool.get("has_gpu_temp") or sensor_pool.get("gpu_temp_load_frac") is not None:
                evidence.append(f"GPU near idle ({src} proxy)")
                gpu_load = 0.0

    if cpu_inferred and cpu_load >= 0.20 and (not gpu_inferred or gpu_load < _IDLE_LOAD_FRAC):
        scores["cpu"] += 1.2 * _PROXY_LOAD_WEIGHT
        if "CPU-only" not in " ".join(evidence):
            evidence.append("CPU-only load (GPU thermals/power near idle)")

    return observed


def compute_workload_fingerprint(
    perf: dict[str, float],
    sensor_pool: dict[str, float | None],
    *,
    title: str = "",
    description: str = "",
) -> dict[str, Any]:
    """
    Multi-dimensional workload scores → proportions + active bottlenecks + evidence.
    """
    scores = {"cpu": 0.0, "gpu": 0.0, "cache": 0.0, "memory": 0.0, "storage": 0.0, "thermal": 0.0}
    evidence: list[str] = []

    hardware_normalized = any(
        sensor_pool.get(k) is not None
        for k in (
            "cpu_usage_load_frac",
            "gpu_usage_load_frac",
            "cpu_temp_load_frac",
            "gpu_temp_load_frac",
            "cpu_power_load_frac",
            "gpu_power_load_frac",
            "cpu_freq_load_frac",
            "cpu_freq_droop_frac",
        )
    )
    cpu_usage_seen = bool(sensor_pool.get("has_cpu_usage"))
    gpu_usage_seen = bool(sensor_pool.get("has_gpu_usage"))
    util_observed = _apply_utilization_scores(scores, evidence, sensor_pool)
    proxy_observed = _apply_proxy_load_scores(
        scores,
        evidence,
        sensor_pool,
        cpu_usage_seen=cpu_usage_seen,
        gpu_usage_seen=gpu_usage_seen,
    )
    sensor_observed = util_observed or proxy_observed

    instr = perf.get("instructions")
    cycles = perf.get("cycles")
    if instr and cycles and cycles > 0:
        ipc = instr / cycles
        if ipc >= 1.2:
            scores["cpu"] += 1.5
            evidence.append(f"IPC≈{ipc:.2f}")
        elif ipc < 0.75:
            scores["memory"] += 1.2
            scores["cache"] += 0.8
            evidence.append(f"low IPC≈{ipc:.2f}")

    refs = perf.get("cache_references")
    misses = perf.get("cache_misses")
    if refs and refs > 0 and misses is not None:
        miss_rate = misses / refs
        if miss_rate >= 0.03:
            scores["cache"] += 2.0 + min(2.5, miss_rate * 12)
            evidence.append(f"cache miss≈{miss_rate * 100:.1f}%")

    branches = perf.get("branch_instructions")
    branch_miss = perf.get("branch_misses")
    if branches and branches > 0 and branch_miss is not None:
        br_miss = branch_miss / branches
        if br_miss >= 0.015:
            scores["cpu"] += 0.8
            evidence.append(f"branch miss≈{br_miss * 100:.1f}%")

    faults = perf.get("page_faults")
    if instr and instr > 0 and faults is not None:
        fault_rate = faults / instr
        if fault_rate >= 1e-5:
            scores["memory"] += 1.5 + min(2.0, fault_rate * 1e6 * 0.5)
            evidence.append(f"page faults/instr≈{fault_rate:.2e}")

    eg = perf.get("energy_gpu", 0) or 0
    ec = perf.get("energy_cores", 0) or 0
    if eg > 0 and ec > 0 and eg / ec >= 0.35:
        scores["gpu"] += 1.2
        evidence.append("energy-gpu elevated vs cores")

    gpu_busy = perf.get("gpu_busy")
    cycles = perf.get("cycles")
    if gpu_busy and gpu_busy > 0:
        if cycles and cycles > 0:
            gpu_busy_ratio = gpu_busy / cycles
            if gpu_busy_ratio >= 0.01:
                scores["gpu"] += 1.5
                evidence.append(f"GPU busy/cycle≈{gpu_busy_ratio:.4f}")
        else:
            scores["gpu"] += 1.0
            evidence.append("GPU active (perf counter)")

    cpu_temp = sensor_pool.get("cpu_temp_peak")
    temp_slope = sensor_pool.get("cpu_temp_slope")
    freq_droop = sensor_pool.get("cpu_freq_droop")
    cpu_temp_frac = sensor_pool.get("cpu_temp_load_frac")
    freq_droop_frac = sensor_pool.get("cpu_freq_droop_frac")
    temp_slope_frac = sensor_pool.get("cpu_temp_slope_frac")

    if cpu_temp_frac is not None:
        if cpu_temp_frac >= 0.60:
            scores["thermal"] += 1.5 + min(1.5, (cpu_temp_frac - 0.60) * 3.0)
            if not proxy_observed or cpu_usage_seen:
                evidence.append(f"CPU temp≈{cpu_temp_frac * 100:.0f}% of model thermal span")
    elif cpu_temp is not None and cpu_temp >= 75:
        scores["thermal"] += 1.5 + min(1.5, (cpu_temp - 75) / 15)
        evidence.append(f"CPU temp peak≈{cpu_temp:.0f}°C")

    if temp_slope_frac is not None and temp_slope_frac >= 0.45:
        scores["thermal"] += 1.0
        evidence.append("CPU temp rising vs model baseline")
    elif temp_slope is not None and temp_slope > 0.15:
        scores["thermal"] += 1.0
        evidence.append("CPU temp rising during run")

    if freq_droop_frac is not None and freq_droop_frac >= 0.55:
        scores["thermal"] += 1.2
        evidence.append(f"CPU freq droop≈{freq_droop_frac * 100:.0f}% of model span")
    elif freq_droop is not None and freq_droop >= 200:
        scores["thermal"] += 1.2
        evidence.append(f"CPU freq droop≈{freq_droop:.0f} MHz")

    blob = _title_blob(title, description)
    title_scores = _title_score_fallback(blob)
    if not sensor_observed:
        for k, v in title_scores.items():
            if v > 0 and scores[k] < 0.5:
                scores[k] += v * 0.35

    core_scores = {k: scores[k] for k in ("cpu", "gpu", "cache", "memory", "storage")}
    if max(core_scores.values()) < 0.5:
        props = score_proportions(core_scores, keys=_WORKLOAD_KEYS)
        return {
            "scores": {k: round(scores[k], 3) for k in scores},
            "proportions": {k: round(props[k], 3) for k in props},
            "thermal_score": round(scores["thermal"], 3),
            "active_bottlenecks": [],
            "scope": "general",
            "confidence": 0.28 if proxy_observed else (0.25 if util_observed else 0.2),
            "evidence": evidence or ["insufficient perf/sensor signal — title prior only"],
            "source": (
                "sensors+thermal_proxy" if proxy_observed and not util_observed
                else ("sensors" if sensor_observed else "title_heuristic")
            ),
            "hardware_normalized": hardware_normalized,
            "load_proxy_used": proxy_observed,
            "insufficient_signal": not sensor_observed and not perf,
        }

    props = score_proportions(core_scores, keys=_WORKLOAD_KEYS)
    active = active_bottlenecks_from_scores(core_scores, keys=_WORKLOAD_KEYS)
    thermal_notable = scores["thermal"] >= 1.0

    if len(active) >= 2:
        scope = "mixed"
    elif active:
        scope = active[0]
    else:
        scope = max(core_scores, key=core_scores.get)

    top = max(props.values()) if props else 0
    second = sorted(props.values(), reverse=True)[1] if len(props) > 1 else 0
    confidence = min(
        0.95,
        0.3 + 0.12 * max(core_scores.values()) + (0.1 if top > second * 1.4 else 0),
    )
    if perf:
        confidence = min(0.95, confidence + 0.1)
    if util_observed:
        confidence = min(0.95, confidence + 0.12)
    elif proxy_observed:
        confidence = min(0.95, confidence + 0.08)
    if hardware_normalized:
        confidence = min(0.95, confidence + 0.05)

    if perf and util_observed:
        source = "perf+sensors"
    elif perf and proxy_observed:
        source = "perf+sensors+thermal_proxy"
    elif perf:
        source = "perf"
    elif util_observed:
        source = "sensors+hardware_baseline" if hardware_normalized else "sensors"
    elif proxy_observed:
        source = "sensors+thermal_proxy"
    else:
        source = "title_heuristic"

    return {
        "scores": {k: round(scores[k], 3) for k in scores},
        "proportions": {k: round(props[k], 3) for k in props},
        "thermal_score": round(scores["thermal"], 3),
        "thermal_notable": thermal_notable,
        "active_bottlenecks": active,
        "scope": scope,
        "confidence": round(confidence, 3),
        "evidence": evidence,
        "source": source,
        "hardware_normalized": hardware_normalized,
        "load_proxy_used": proxy_observed,
        "insufficient_signal": False,
    }
