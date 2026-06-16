"""Tests for incremental insights rebuild helpers."""

import unittest
from datetime import datetime, timedelta

from app import create_app, db
from app.insights_util import benchmark_group_has_new_data, benchmark_group_needs_rebuild
from app.models import Benchmark, BenchmarkAnalysis, BenchmarkResult, System


class InsightsUtilTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_incremental_skips_unchanged_group(self):
        system = System(identifier="rig-a", hardware="Processor: X", software="OS")
        db.session.add(system)
        db.session.flush()

        benchmark = Benchmark(
            identifier="pts",
            title="Example Bench",
            app_version="1.0",
            display_format="BAR_GRAPH",
            is_primary=True,
        )
        db.session.add(benchmark)
        db.session.flush()

        imported_at = datetime.utcnow() - timedelta(hours=2)
        result = BenchmarkResult(
            system_id=system.id,
            benchmark_id=benchmark.id,
            value=100.0,
            imported_at=imported_at,
        )
        db.session.add(result)

        analysis = BenchmarkAnalysis(
            benchmark_identifier="pts",
            benchmark_title="Example Bench",
            benchmark_app_version="1.0",
            analysis_json={"default": {}},
            last_updated=datetime.utcnow() - timedelta(hours=1),
        )
        db.session.add(analysis)
        db.session.commit()

        bm_list = [benchmark]
        self.assertFalse(
            benchmark_group_needs_rebuild(
                "Example Bench",
                "1.0",
                bm_list,
                incremental=True,
            )
        )

    def test_incremental_rebuilds_when_new_results_arrive(self):
        system = System(identifier="rig-a", hardware="Processor: X", software="OS")
        db.session.add(system)
        db.session.flush()

        benchmark = Benchmark(
            identifier="pts",
            title="Example Bench",
            app_version="1.0",
            display_format="BAR_GRAPH",
            is_primary=True,
        )
        db.session.add(benchmark)
        db.session.flush()

        old_import = datetime.utcnow() - timedelta(hours=3)
        new_import = datetime.utcnow() - timedelta(minutes=5)
        db.session.add(
            BenchmarkResult(
                system_id=system.id,
                benchmark_id=benchmark.id,
                value=100.0,
                imported_at=old_import,
            )
        )
        analysis = BenchmarkAnalysis(
            benchmark_identifier="pts",
            benchmark_title="Example Bench",
            benchmark_app_version="1.0",
            analysis_json={"default": {}},
            last_updated=datetime.utcnow() - timedelta(hours=2),
        )
        db.session.add(analysis)
        db.session.commit()

        db.session.add(
            BenchmarkResult(
                system_id=system.id,
                benchmark_id=benchmark.id,
                value=110.0,
                imported_at=new_import,
            )
        )
        db.session.commit()

        self.assertTrue(benchmark_group_has_new_data([benchmark], analysis.last_updated))


if __name__ == "__main__":
    unittest.main()
