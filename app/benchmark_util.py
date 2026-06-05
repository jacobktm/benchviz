"""Shared helpers for benchmark definition lookup and orphan cleanup."""

from . import db
from .models import Benchmark, BenchmarkResult


def _norm(value):
    if value is None:
        return ''
    return str(value).strip()


def find_benchmark_definition(identifier, title, app_version, description, scale):
    """Look up a benchmark row using the same fields as uix_benchmark_def."""
    identifier = _norm(identifier)
    title = _norm(title)
    app_version = _norm(app_version)
    description = _norm(description)
    scale = _norm(scale)

    return Benchmark.query.filter(
        db.func.coalesce(Benchmark.identifier, '') == identifier,
        Benchmark.title == title,
        db.func.coalesce(Benchmark.app_version, '') == app_version,
        db.func.coalesce(Benchmark.description, '') == description,
        db.func.coalesce(Benchmark.scale, '') == scale,
    ).first()


def get_or_create_benchmark(
    identifier,
    title,
    app_version,
    description,
    scale,
    proportion,
    display_format,
    is_primary,
):
    benchmark = find_benchmark_definition(
        identifier, title, app_version, description, scale
    )
    if benchmark:
        benchmark.proportion = proportion
        benchmark.display_format = display_format
        benchmark.is_primary = is_primary
        return benchmark

    benchmark = Benchmark(
        identifier=_norm(identifier) or None,
        title=_norm(title),
        app_version=_norm(app_version) or None,
        description=_norm(description) or None,
        scale=_norm(scale) or None,
        proportion=proportion,
        display_format=display_format,
        is_primary=is_primary,
    )
    db.session.add(benchmark)
    db.session.flush()
    return benchmark


def delete_orphan_benchmarks():
    """Remove benchmark definitions that no longer have any results."""
    orphans = Benchmark.query.filter(~Benchmark.results.any()).all()
    for benchmark in orphans:
        db.session.delete(benchmark)
    return len(orphans)


def delete_system_benchmark_suite(system_id, title, app_version, identifier=None):
    """
    Delete all benchmark results for one suite on a system (primary + sensors).
    Returns the number of result rows removed.
    """
    identifier = _norm(identifier)
    suite_query = Benchmark.query.filter(Benchmark.title == title)
    if app_version is not None:
        suite_query = suite_query.filter(
            db.func.coalesce(Benchmark.app_version, '') == _norm(app_version)
        )
    if identifier:
        suite_query = suite_query.filter(
            db.func.coalesce(Benchmark.identifier, '') == identifier
        )
    else:
        suite_query = suite_query.filter(
            db.or_(
                Benchmark.identifier.is_(None),
                Benchmark.identifier == '',
            )
        )

    benchmark_ids = [b.id for b in suite_query.all()]
    if not benchmark_ids:
        return 0

    deleted = BenchmarkResult.query.filter(
        BenchmarkResult.system_id == system_id,
        BenchmarkResult.benchmark_id.in_(benchmark_ids),
    ).delete(synchronize_session=False)
    delete_orphan_benchmarks()
    return deleted
