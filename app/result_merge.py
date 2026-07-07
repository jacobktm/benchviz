"""
Helpers for benchmark result rows: per-run scalars and upload-batch assignment.

Each XML upload creates new ``BenchmarkResult`` rows (one per metric) sharing an
``import_batch_id`` and frozen ``profile_snapshot`` so cooler/PSU changes stay distinct.
"""

from __future__ import annotations

import json
import re
import statistics
from typing import Any

from app.models import BenchmarkResult

_RUN_TOKEN_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")


def extract_run_values_from_entry(entry_node) -> list[float]:
    """Parse per-run scalars from a Phoronix Result/Data/Entry node."""
    run_values: list[float] = []
    raw_run_str = entry_node.findtext("RawString", default="") or ""
    if raw_run_str.strip():
        for t in _RUN_TOKEN_RE.findall(raw_run_str):
            try:
                run_values.append(float(t))
            except (ValueError, TypeError):
                pass
    if not run_values:
        json_text = entry_node.findtext("JSON", default="") or ""
        if json_text.strip():
            try:
                parsed = json.loads(json_text)
                if isinstance(parsed, dict):
                    # If the JSON contains an "error" field, the test failed.
                    # Its run times are execution durations with no benchmark
                    # meaning; discard the entry entirely.
                    error_val = parsed.get("error")
                    if error_val and isinstance(error_val, str) and error_val.strip():
                        return []
                    candidate_keys = [
                        k for k in parsed.keys()
                        if isinstance(k, str) and ("test-run-times" in k or "run-times" in k or "run_times" in k)
                    ]
                    for ck in candidate_keys:
                        v = parsed.get(ck)
                        if isinstance(v, str) and v.strip():
                            for t in _RUN_TOKEN_RE.findall(v):
                                try:
                                    run_values.append(float(t))
                                except (ValueError, TypeError):
                                    pass
                            if run_values:
                                break
            except Exception:
                pass
    return run_values


def bar_run_values(data_json: Any, value: float | None = None) -> list[float]:
    """BAR_GRAPH per-run scalars on one result row (flat list)."""
    out: list[float] = []
    if isinstance(data_json, list):
        for v in data_json:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(float(v))
    if not out and value is not None:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            pass
    return out


def assign_bar_graph_result(
    b_result: BenchmarkResult,
    entry_node,
    value_str: str | None,
) -> None:
    run_values = extract_run_values_from_entry(entry_node)
    if not run_values and value_str:
        try:
            run_values = [float(value_str)]
        except (ValueError, TypeError):
            run_values = []
    if run_values:
        b_result.data_json = run_values
        b_result.value = statistics.mean(run_values)
    elif value_str:
        try:
            b_result.value = float(value_str)
            b_result.data_json = [b_result.value]
        except (ValueError, TypeError):
            b_result.value = None


def assign_line_graph_result(b_result: BenchmarkResult, series: list[float]) -> None:
    b_result.data_json = list(series) if series else None


def observation_batch_id(result: BenchmarkResult) -> str:
    """Stable key for one upload observation (system + batch)."""
    batch = (result.import_batch_id or "").strip()
    if batch:
        return batch
    return f"legacy-{result.id}"


def aggregate_bar_runs_across_results(results: list[BenchmarkResult]) -> list[float]:
    """Combine per-run scalars from multiple result rows (e.g. same system, many uploads)."""
    out: list[float] = []
    for res in results:
        out.extend(bar_run_values(res.data_json, res.value))
    return out
