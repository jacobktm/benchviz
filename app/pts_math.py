"""Phoronix Test Suite math helpers (ported from pts-core/objects/pts_math.php)."""

from __future__ import annotations

import math
from typing import Sequence


def geometric_mean(values: Sequence[float]) -> float | None:
    """PTS chunked geometric mean (avoids overflow on large arrays)."""
    nums = [float(v) for v in values if v is not None and math.isfinite(float(v)) and float(v) > 0]
    if not nums:
        return None
    if len(nums) == 1:
        return nums[0]
    power = 1.0 / len(nums)
    chunk_results: list[float] = []
    for i in range(0, len(nums), 8):
        chunk = nums[i : i + 8]
        chunk_results.append(math.pow(math.prod(chunk), power))
    return math.prod(chunk_results)


def result_to_percentile(value: float, percentiles: Sequence[float], hib: bool) -> int | None:
    """
    Map a result to an OpenBenchmarking population percentile (1–100).

    Mirrors pts_ae_data::result_to_percentile().
    """
    if not percentiles or value is None or not math.isfinite(float(value)):
        return None
    pct_list = list(percentiles)
    if not hib:
        pct_list = list(reversed(pct_list))
    v = float(value)
    for i, threshold in enumerate(pct_list):
        if hib and v < float(threshold):
            return i + 1
        if not hib and v > float(threshold):
            return i + 1
    if hib:
        return 100
    return None
