"""
Blend spec-based `rank_value_spec` with empirical primary benchmark performance per part.

Percentiles are computed within each (benchmark × arguments) arm so cache-sensitive
workloads naturally differentiate e.g. 9950X vs 9950X3D when your data says so.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import joinedload

from app.components import get_system_components, hardware_rank_match_key
from app.models import Benchmark, BenchmarkResult, HardwareTheoreticalRank


def _proportion_is_lower_better(p: str | None) -> bool:
    x = (p or "").strip().upper()
    return x == "LIB"


def _performance_scalar(value: float, lower_better: bool) -> float:
    """Higher return value = better outcome (for ranking / percentiles)."""
    v = float(value)
    return -v if lower_better else v


def collect_empirical_percentiles_by_match_key(
    part_kind: str,
) -> dict[str, list[float]]:
    """
    For each benchmark arm, assign each system a percentile (0=worst .. 1=best among systems
    in that arm for the same primary scalar). Average percentiles for systems sharing a
    match_key in that arm. Return match_key -> list of per-arm means (one entry per arm
    where the part appeared).
    """
    kind = (part_kind or "").strip().lower()
    if kind not in ("cpu", "gpu"):
        return {}

    feat = "processor" if kind == "cpu" else "graphics"
    comp_key = "processor" if kind == "cpu" else "graphics"

    results = (
        BenchmarkResult.query.options(
            joinedload(BenchmarkResult.benchmark),
            joinedload(BenchmarkResult.system),
        )
        .join(Benchmark, BenchmarkResult.benchmark_id == Benchmark.id)
        .filter(
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary.is_(True),
        )
        .all()
    )

    arms: dict[tuple[int, str], list[BenchmarkResult]] = defaultdict(list)
    for r in results:
        if r.value is None or r.system is None or r.benchmark is None:
            continue
        key = (r.benchmark_id, (r.arguments or ""))
        arms[key].append(r)

    by_mk_lists: dict[str, list[float]] = defaultdict(list)

    for (_bid, _arg), rlist in arms.items():
        if len(rlist) < 2:
            continue
        b0 = rlist[0].benchmark
        lower_better = _proportion_is_lower_better(b0.proportion if b0 else None)

        rows: list[tuple[str, int, float]] = []
        for r in rlist:
            comps = get_system_components(r.system)
            raw = (comps.get(comp_key) or "").strip()
            mk = hardware_rank_match_key(feat, raw) if raw else ""
            if not mk:
                continue
            try:
                perf = _performance_scalar(float(r.value), lower_better)
            except (TypeError, ValueError):
                continue
            rows.append((mk, r.system_id, perf))

        if len(rows) < 2:
            continue

        rows_sorted = sorted(rows, key=lambda t: t[2])
        n = len(rows_sorted)
        perf_to_ranks: dict[float, list[int]] = defaultdict(list)
        for idx, (_mk, _sid, perf) in enumerate(rows_sorted):
            perf_to_ranks[perf].append(idx)

        system_pct: dict[int, float] = {}
        for _perf, idxs in perf_to_ranks.items():
            avg_rank = sum(idxs) / len(idxs)
            pcent = avg_rank / max(n - 1, 1)
            for i in idxs:
                sid = rows_sorted[i][1]
                system_pct[sid] = pcent

        mk_vals: dict[str, list[float]] = defaultdict(list)
        for mk, sid, _perf in rows_sorted:
            if sid in system_pct:
                mk_vals[mk].append(system_pct[sid])

        for mk, plist in mk_vals.items():
            if plist:
                by_mk_lists[mk].append(float(statistics.mean(plist)))

    return dict(by_mk_lists)


def median_empirical_index(per_arm_means: list[float]) -> float:
    if not per_arm_means:
        return 0.5
    return float(statistics.median(per_arm_means))


def calibrate_hardware_ranks(
    spec_weight: float = 0.35,
    part_kind: str = "both",
) -> dict[str, Any]:
    """
    Update `HardwareTheoreticalRank.rank_value` from rank_value_spec and primary benchmarks.

    Blended index = spec_weight * norm(spec) + (1 - spec_weight) * empirical_median
    where empirical is median across benchmark arms of mean within-arm percentiles.
    Parts with no bench data use norm(spec) for the empirical term (no change in blend).

    Returns counters and diagnostics.
    """
    sw = max(0.0, min(1.0, float(spec_weight)))
    pk = (part_kind or "both").strip().lower()
    if pk not in ("both", "cpu", "gpu"):
        return {
            "updated": 0,
            "spec_weight": sw,
            "error": "part_kind must be cpu, gpu, or both",
            "detail": {},
        }
    kinds: tuple[str, ...] = ("cpu", "gpu") if pk == "both" else (pk,)

    updated = 0
    detail: dict[str, Any] = {"kinds": {}}

    for kind in kinds:
        rows = HardwareTheoreticalRank.query.filter_by(part_kind=kind).all()
        if not rows:
            detail["kinds"][kind] = {"rows": 0, "with_bench_signal": 0, "match_keys_with_empirical": 0}
            continue

        spec_vals = []
        for r in rows:
            base = r.rank_value_spec
            if base is None:
                base = r.rank_value
            spec_vals.append(float(base))
        s_min = min(spec_vals)
        s_max = max(spec_vals)
        denom = (s_max - s_min) or 1e-12

        emp_lists = collect_empirical_percentiles_by_match_key(kind)
        with_signal = 0

        for rec in rows:
            spec_base = rec.rank_value_spec
            if spec_base is None:
                spec_base = rec.rank_value
            spec_f = float(spec_base)
            spec_norm = (spec_f - s_min) / denom

            plist = emp_lists.get(rec.match_key, [])
            if plist:
                with_signal += 1
                emp_idx = median_empirical_index(plist)
            else:
                emp_idx = spec_norm

            blend = sw * spec_norm + (1.0 - sw) * emp_idx
            blend = max(0.0, min(1.0, blend))
            rec.rank_value = 1.0 + blend * 999_998.0 + spec_f * 1e-9

            base_note = (rec.source_note or "").split("| cal ", 1)[0].strip()
            cal_bit = f"cal sw={sw:.2f} emp_n={len(plist)}"
            note = f"{base_note} | cal {cal_bit}" if base_note else cal_bit
            rec.source_note = note[:255]

            updated += 1

        detail["kinds"][kind] = {
            "rows": len(rows),
            "with_bench_signal": with_signal,
            "match_keys_with_empirical": len(emp_lists),
        }

    return {"updated": updated, "spec_weight": sw, "detail": detail}
