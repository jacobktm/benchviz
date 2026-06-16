"""Shared helpers for incremental insights rebuilds."""

from __future__ import annotations

from app import db
from app.models import BenchmarkAnalysis, BenchmarkResult


def analysis_last_updated(title: str, app_version: str):
    """Latest rebuild timestamp for a benchmark group, or None if never analyzed."""
    rows = BenchmarkAnalysis.query.filter_by(
        benchmark_title=title,
        benchmark_app_version=app_version,
    ).all()
    if not rows:
        return None
    times = [row.last_updated for row in rows if row.last_updated is not None]
    return max(times) if times else None


def benchmark_group_has_new_data(bm_list, since) -> bool:
    """True when any result in the group was imported after `since`."""
    if since is None:
        return True
    bm_ids = [bm.id for bm in bm_list]
    if not bm_ids:
        return False
    return (
        db.session.query(BenchmarkResult.id)
        .filter(
            BenchmarkResult.benchmark_id.in_(bm_ids),
            BenchmarkResult.imported_at > since,
        )
        .first()
        is not None
    )


def benchmark_group_needs_rebuild(title: str, app_version: str, bm_list, *, incremental: bool) -> bool:
    if not incremental:
        return True
    since = analysis_last_updated(title, app_version)
    if since is None:
        return True
    return benchmark_group_has_new_data(bm_list, since)
