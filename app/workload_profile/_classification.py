"""Workload classification — bottleneck inference from perf + sensor signals."""

from __future__ import annotations

from typing import Any

from ..workload_consensus import active_bottlenecks_from_scores, score_proportions
from ._constants import SCOPE_HARDWARE_KEYS, SCOPE_SENSOR_KEYWORDS


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

    gpu_busy = perf.get("gpu_busy")
    cycles = perf.get("cycles")
    if gpu_busy and gpu_busy > 0:
        if cycles and cycles > 0 and gpu_busy / cycles >= 0.01:
            scores["gpu"] += 2.0
            evidence.append(f"GPU busy/cycle≈{gpu_busy / cycles:.4f}")
        elif not cycles:
            scores["gpu"] += 1.5
            evidence.append("GPU active (perf counter)")

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
