"""
Workload fingerprint: CPU / GPU / cache / memory / storage / thermal dominance.

Uses perf counters and MONITOR sensors (usage and thermal kept separate).
Outputs normalized proportions suitable for Insights routing and ML storage.
"""

from __future__ import annotations

import math
from typing import Any

from app.workload_consensus import MIN_ACTIVE_SHARE


def _score_proportions(scores: dict[str, float]) -> dict[str, float]:
    keys = ("cpu", "gpu", "cache", "memory", "storage")
    total = sum(max(0.0, float(scores.get(k, 0) or 0)) for k in keys)
    if total <= 0:
        return {k: 1.0 / len(keys) for k in keys}
    return {k: max(0.0, float(scores.get(k, 0) or 0)) / total for k in keys}


def _active_bottlenecks(scores: dict[str, float]) -> list[str]:
    props = _score_proportions(scores)
    active = [k for k in props if props[k] >= MIN_ACTIVE_SHARE]
    if active:
        return sorted(active, key=lambda k: -props[k])
    peak = max((float(scores.get(k, 0) or 0) for k in props), default=0.0)
    if peak <= 0:
        return []
    return sorted(
        [k for k in props if float(scores.get(k, 0) or 0) >= peak * 0.4],
        key=lambda k: -float(scores.get(k, 0) or 0),
    )


def _title_blob(title: str, description: str = "") -> str:
    return f"{title} {description}".lower()


def _title_scope_fallback(blob: str) -> dict[str, float]:
    scores = {"cpu": 0.0, "gpu": 0.0, "cache": 0.0, "memory": 0.0, "storage": 0.0, "thermal": 0.0}
    if any(k in blob for k in ("vulkan", "cuda", "opengl", "render", "graphics", "gpu ")):
        scores["gpu"] += 2.0
    if any(k in blob for k in ("nvme", "disk", "io", "storage", "ssd", "fio")):
        scores["storage"] += 2.0
    if any(k in blob for k in ("compile", "compression", "encode", "kernel", "openssl")):
        scores["cpu"] += 1.0
    if any(k in blob for k in ("stream", "memory", "ram bandwidth")):
        scores["memory"] += 2.0
    return scores


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

    cpu_usage = sensor_pool.get("cpu_usage_peak")
    gpu_usage = sensor_pool.get("gpu_usage_peak")
    if gpu_usage is not None and gpu_usage >= 35:
        scores["gpu"] += 3.0
        evidence.append(f"GPU usage peak≈{gpu_usage:.0f}%")
    elif gpu_usage is not None and gpu_usage >= 12:
        scores["gpu"] += 1.5
    if cpu_usage is not None and cpu_usage >= 65 and (gpu_usage is None or gpu_usage < 25):
        scores["cpu"] += 2.0
        evidence.append(f"CPU usage peak≈{cpu_usage:.0f}%")
    elif cpu_usage is not None and cpu_usage >= 40:
        scores["cpu"] += 1.0

    eg = perf.get("energy_gpu", 0) or 0
    ec = perf.get("energy_cores", 0) or 0
    if eg > 0 and ec > 0 and eg / ec >= 0.35:
        scores["gpu"] += 1.2
        evidence.append("energy-gpu elevated vs cores")

    cpu_temp = sensor_pool.get("cpu_temp_peak")
    temp_slope = sensor_pool.get("cpu_temp_slope")
    freq_droop = sensor_pool.get("cpu_freq_droop")
    if cpu_temp is not None and cpu_temp >= 75:
        scores["thermal"] += 1.5 + min(1.5, (cpu_temp - 75) / 15)
        evidence.append(f"CPU temp peak≈{cpu_temp:.0f}°C")
    if temp_slope is not None and temp_slope > 0.15:
        scores["thermal"] += 1.0
        evidence.append(f"CPU temp rising during run")
    if freq_droop is not None and freq_droop >= 200:
        scores["thermal"] += 1.2
        evidence.append(f"CPU freq droop≈{freq_droop:.0f} MHz")

    blob = _title_blob(title, description)
    title_scores = _title_scope_fallback(blob)
    for k, v in title_scores.items():
        if v > 0 and scores[k] < 0.5:
            scores[k] += v * 0.35

    if max(scores.values()) < 0.5:
        props = _score_proportions({k: scores[k] for k in ("cpu", "gpu", "cache", "memory", "storage")})
        return {
            "scores": {k: round(scores[k], 3) for k in scores},
            "proportions": {k: round(props[k], 3) for k in props},
            "thermal_score": round(scores["thermal"], 3),
            "active_bottlenecks": [],
            "scope": "general",
            "confidence": 0.2,
            "evidence": evidence or ["insufficient perf/sensor signal — title prior only"],
            "source": "title_heuristic",
        }

    core_scores = {k: scores[k] for k in ("cpu", "gpu", "cache", "memory", "storage")}
    props = _score_proportions(core_scores)
    active = _active_bottlenecks(core_scores)
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
    if sensor_pool.get("cpu_usage_peak") is not None or sensor_pool.get("gpu_usage_peak") is not None:
        confidence = min(0.95, confidence + 0.08)

    source = "perf+sensors" if perf else ("sensors" if any(sensor_pool.values()) else "title_heuristic")

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
    }
