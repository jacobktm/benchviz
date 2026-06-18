from __future__ import annotations

from app.models import Benchmark


class BenchmarkRepository:
    @staticmethod
    def find_primary_by_title(title: str, app_version: str | None = None) -> list[Benchmark]:
        q = Benchmark.query.filter(
            Benchmark.title == title,
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary.is_(True),
        )
        if app_version:
            q = q.filter(Benchmark.app_version == app_version)
        return q.all()

    @staticmethod
    def find_primary_with_results(title: str, app_version: str | None = None) -> list[Benchmark]:
        q = Benchmark.query.filter(
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary.is_(True),
            Benchmark.results.any(),
        )
        if title:
            q = q.filter(Benchmark.title == title)
        if app_version:
            q = q.filter(Benchmark.app_version == app_version)
        return q.all()

    @staticmethod
    def find_perf_counters_by_title(title: str, app_version: str | None = None) -> list[Benchmark]:
        q = Benchmark.query.filter(
            Benchmark.title == title,
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary.is_(False),
        )
        if app_version:
            q = q.filter(Benchmark.app_version == app_version)
        return q.all()

    @staticmethod
    def find_sensors_by_title(title: str, app_version: str | None = None) -> list[Benchmark]:
        q = Benchmark.query.filter(
            Benchmark.title == title,
            Benchmark.display_format == "LINE_GRAPH",
        )
        if app_version:
            q = q.filter(Benchmark.app_version == app_version)
        return q.all()

    @staticmethod
    def find_by_ids(bm_ids: list[int]) -> dict[int, Benchmark]:
        benchmarks = Benchmark.query.filter(Benchmark.id.in_(bm_ids)).all()
        return {b.id: b for b in benchmarks}

    @staticmethod
    def find_first_primary(title: str, app_version: str | None = None) -> Benchmark | None:
        q = Benchmark.query.filter(
            Benchmark.title == title,
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary == True,
        )
        if app_version:
            q = q.filter(Benchmark.app_version == app_version)
        return q.first()

    @staticmethod
    def find_all_primary() -> list[Benchmark]:
        return Benchmark.query.filter(
            Benchmark.display_format == "BAR_GRAPH",
            Benchmark.is_primary.is_(True),
        ).all()

    @staticmethod
    def find_all_with_results() -> list[Benchmark]:
        return Benchmark.query.filter(Benchmark.results.any()).all()
