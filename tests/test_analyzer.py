"""Tests for legacy statistical analysis of benchmark results."""

import datetime
import time
import unittest

from app import create_app, db
from app.models import Benchmark, BenchmarkAnalysis, BenchmarkResult, System
from app.analyzer import analyze_benchmarks, MIN_SYSTEMS_TOTAL


class AnalyzerTest(unittest.TestCase):
    """Tests for analyze_benchmarks()."""

    def setUp(self):
        self.app = create_app()
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    # ── helpers ────────────────────────────────────────────────────

    def _system(self, identifier: str, hardware: str, software: str,
                **kw) -> System:
        sys = System(
            identifier=identifier,
            hardware=hardware,
            software=software,
            user='tester',
            timestamp='2026-01-01',
            **kw,
        )
        db.session.add(sys)
        db.session.flush()
        return sys

    def _benchmark(self, identifier: str, title: str, **kw) -> Benchmark:
        bm = Benchmark(
            identifier=identifier,
            title=title,
            app_version=kw.pop('app_version', '1.0'),
            description=kw.pop('description', 'Test benchmark'),
            scale=kw.pop('scale', 'Seconds'),
            proportion=kw.pop('proportion', 'LIB'),
            display_format=kw.pop('display_format', 'BAR_GRAPH'),
            is_primary=kw.pop('is_primary', True),
            **kw,
        )
        db.session.add(bm)
        db.session.flush()
        return bm

    def _result(self, system: System, benchmark: Benchmark, value: float,
                arguments: str = 'default') -> BenchmarkResult:
        r = BenchmarkResult(
            system_id=system.id,
            benchmark_id=benchmark.id,
            value=value,
            arguments=arguments,
        )
        db.session.add(r)
        db.session.flush()
        return r

    def _features(self, payload, arg='default'):
        """Return the feature-stats dict for a given argument group."""
        return payload.get(arg, {})

    # ── baseline setup for feature-analysis tests ──────────────────

    def _setup_three_systems(self):
        """3 systems with different processors, same benchmark, each with 1 result."""
        sys_a = self._system(
            'sys-a',
            'Processor: CPU Alpha, Memory: 16GB',
            'OS: Linux 6.8',
        )
        sys_b = self._system(
            'sys-b',
            'Processor: CPU Beta, Memory: 32GB',
            'OS: Linux 6.9',
        )
        sys_c = self._system(
            'sys-c',
            'Processor: CPU Gamma, Memory: 64GB',
            'OS: Linux 6.10',
        )
        bm = self._benchmark('pts/bench-1.0.0', 'Bench', scale='Seconds')
        self._result(sys_a, bm, 10.0)
        self._result(sys_b, bm, 20.0)
        self._result(sys_c, bm, 30.0)
        db.session.commit()
        return sys_a, sys_b, sys_c, bm

    # ── empty / no-op ─────────────────────────────────────────────

    def test_empty_database_no_crash(self):
        analyze_benchmarks(incremental=False)
        self.assertEqual(BenchmarkAnalysis.query.count(), 0)

    def test_no_primary_bar_graph_no_analysis(self):
        sys = self._system('sys', 'Processor: CPU', 'OS: Linux')
        bm = self._benchmark('pts/non-primary-1.0.0', 'NP',
                             is_primary=False)
        self._result(sys, bm, 42.0)
        db.session.commit()
        analyze_benchmarks(incremental=False)
        self.assertEqual(BenchmarkAnalysis.query.count(), 0)

    def test_non_bar_graph_skipped(self):
        sys = self._system('sys', 'Processor: CPU', 'OS: Linux')
        bm = self._benchmark('pts/line-1.0.0', 'Line',
                             display_format='LINE_GRAPH')
        self._result(sys, bm, 42.0)
        db.session.commit()
        analyze_benchmarks(incremental=False)
        self.assertEqual(BenchmarkAnalysis.query.count(), 0)

    def test_insufficient_systems_returns_error_feature(self):
        """With only 2 systems, feature stats should contain an error."""
        sys_a = self._system(
            'sys-a', 'Processor: CPU Alpha', 'OS: Linux'
        )
        sys_b = self._system(
            'sys-b', 'Processor: CPU Beta', 'OS: Linux'
        )
        bm = self._benchmark('pts/bench-1.0.0', 'Bench', scale='Seconds')
        self._result(sys_a, bm, 10.0)
        self._result(sys_b, bm, 20.0)
        db.session.commit()
        analyze_benchmarks(incremental=False)
        analyses = BenchmarkAnalysis.query.all()
        self.assertEqual(len(analyses), 1)
        feats = self._features(analyses[0].analysis_json)
        # Some features (processor, memory, os) have only 2 distinct values
        # across < 3 systems → should have error
        found = False
        for stats in feats.values():
            if isinstance(stats, list) and len(stats) == 1 and 'error' in stats[0]:
                found = True
                break
        self.assertTrue(found, 'Expected an error for insufficient data')

    # ── full analysis ─────────────────────────────────────────────

    def test_full_analysis_creates_analysis_record(self):
        self._setup_three_systems()
        analyze_benchmarks(incremental=False)
        self.assertEqual(BenchmarkAnalysis.query.count(), 1)

    def test_full_analysis_sets_metadata(self):
        self._setup_three_systems()
        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        self.assertEqual(a.benchmark_title, 'Bench')
        self.assertEqual(a.benchmark_app_version, '1.0')
        self.assertEqual(a.benchmark_identifier, 'pts/bench-1.0.0')

    def test_full_analysis_includes_processor_feature(self):
        self._setup_three_systems()
        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        self.assertIn('processor', feats)
        stats = feats['processor']
        self.assertEqual(len(stats), 3)
        names = {s['name'] for s in stats}
        self.assertEqual(names, {'CPU Alpha', 'CPU Beta', 'CPU Gamma'})

    def test_full_analysis_includes_memory_feature(self):
        self._setup_three_systems()
        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        self.assertIn('memory', feats)
        names = {s['name'] for s in feats['memory']}
        self.assertEqual(names, {'16GB', '32GB', '64GB'})

    def test_full_analysis_includes_os_feature(self):
        self._setup_three_systems()
        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        self.assertIn('os', feats)
        names = {s['name'] for s in feats['os']}
        self.assertEqual(names, {'Linux 6.8', 'Linux 6.9', 'Linux 6.10'})

    def test_stats_mean_correct(self):
        """Each processor has exactly 1 result, so mean == value."""
        self._setup_three_systems()
        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        stats = feats['processor']
        values = {s['name']: s['mean'] for s in stats}
        self.assertAlmostEqual(values['CPU Alpha'], 10.0)
        self.assertAlmostEqual(values['CPU Beta'], 20.0)
        self.assertAlmostEqual(values['CPU Gamma'], 30.0)

    def test_stats_median_min_max(self):
        self._setup_three_systems()
        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        stats = feats['processor']
        for s in stats:
            self.assertAlmostEqual(s['median'], s['mean'])
            self.assertAlmostEqual(s['min'], s['mean'])
            self.assertAlmostEqual(s['max'], s['mean'])
            self.assertEqual(s['n'], 1)

    def test_lower_is_better_ordering(self):
        """LIB proportion: lower mean = first in sorted list."""
        self._setup_three_systems()
        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        stats = feats['processor']
        means = [s['mean'] for s in stats]
        self.assertEqual(means, sorted(means))  # ascending = lower is better

    def test_higher_is_better_ordering(self):
        sys_a = self._system('sys-a', 'Processor: CPU A', 'OS: Linux')
        sys_b = self._system('sys-b', 'Processor: CPU B', 'OS: Linux')
        sys_c = self._system('sys-c', 'Processor: CPU C', 'OS: Linux')
        bm = self._benchmark('pts/fps-1.0.0', 'FPS', scale='FPS',
                             proportion='HIB')
        self._result(sys_a, bm, 30.0)
        self._result(sys_b, bm, 60.0)
        self._result(sys_c, bm, 90.0)
        db.session.commit()
        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        stats = feats['processor']
        means = [s['mean'] for s in stats]
        self.assertEqual(means, sorted(means, reverse=True))  # descending

    # ── multiple argument groups ──────────────────────────────────

    def test_multiple_argument_groups(self):
        sys = self._system('sys', 'Processor: CPU', 'OS: Linux')
        bm = self._benchmark('pts/bench-1.0.0', 'Bench')
        sys_b = self._system('sys-b', 'Processor: CPU', 'OS: Linux')
        sys_c = self._system('sys-c', 'Processor: CPU Other', 'OS: Linux')
        self._result(sys, bm, 10.0, arguments='low')
        self._result(sys, bm, 20.0, arguments='high')
        self._result(sys_b, bm, 12.0, arguments='low')
        self._result(sys_b, bm, 22.0, arguments='high')
        self._result(sys_c, bm, 11.0, arguments='low')
        self._result(sys_c, bm, 21.0, arguments='high')
        db.session.commit()

        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        payload = a.analysis_json
        self.assertIn('low', payload)
        self.assertIn('high', payload)
        self.assertNotIn('default', payload)

    # ── workload profile included ─────────────────────────────────

    def test_workload_profile_included(self):
        self._setup_three_systems()
        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        payload = a.analysis_json
        self.assertIn('_workload', payload)
        self.assertIn('_workload_by_args', payload)
        self.assertIn('_workload_by_option', payload)

    # ── incremental: skips unchanged ──────────────────────────────

    def test_incremental_skips_unchanged_group(self):
        self._setup_three_systems()
        analyze_benchmarks(incremental=True)
        self.assertEqual(BenchmarkAnalysis.query.count(), 1)

        analyze_benchmarks(incremental=True)
        self.assertEqual(BenchmarkAnalysis.query.count(), 1)

    def test_incremental_runs_for_new_data(self):
        self._setup_three_systems()
        analyze_benchmarks(incremental=True)
        self.assertEqual(BenchmarkAnalysis.query.count(), 1)

        # Force the analysis last_updated to the past so the new result is seen as newer
        analysis = BenchmarkAnalysis.query.first()
        analysis.last_updated = datetime.datetime(2020, 1, 1)
        db.session.commit()

        sys_d = self._system('sys-d', 'Processor: CPU Delta', 'OS: Linux')
        bm = Benchmark.query.first()
        r = self._result(sys_d, bm, 40.0)
        r.imported_at = datetime.datetime(2026, 1, 1)
        db.session.commit()

        analyze_benchmarks(incremental=True)
        self.assertEqual(BenchmarkAnalysis.query.count(), 1)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        names = {s['name'] for s in feats['processor']}
        self.assertIn('CPU Delta', names)

    # ── multiple benchmark groups ─────────────────────────────────

    def test_multiple_benchmark_groups(self):
        sys_a = self._system('sys-a', 'Processor: CPU A', 'OS: Linux')
        sys_b = self._system('sys-b', 'Processor: CPU B', 'OS: Linux')
        sys_c = self._system('sys-c', 'Processor: CPU C', 'OS: Linux')

        bm1 = self._benchmark('pts/alpha-1.0.0', 'Alpha')
        bm2 = self._benchmark('pts/beta-1.0.0', 'Beta')
        for sys in (sys_a, sys_b, sys_c):
            self._result(sys, bm1, 10.0)
            self._result(sys, bm2, 20.0)
        db.session.commit()

        analyze_benchmarks(incremental=False)
        self.assertEqual(BenchmarkAnalysis.query.count(), 2)
        titles = {a.benchmark_title for a in BenchmarkAnalysis.query.all()}
        self.assertEqual(titles, {'Alpha', 'Beta'})

    # ── value with multiple runs per system ───────────────────────

    def test_multiple_results_per_system_are_averaged(self):
        sys = self._system('sys-a', 'Processor: CPU A', 'OS: Linux')
        sys_b = self._system('sys-b', 'Processor: CPU B', 'OS: Linux')
        sys_c = self._system('sys-c', 'Processor: CPU C', 'OS: Linux')
        bm = self._benchmark('pts/bench-1.0.0', 'Bench')
        self._result(sys, bm, 8.0)
        self._result(sys, bm, 12.0)
        self._result(sys_b, bm, 20.0)
        self._result(sys_c, bm, 30.0)
        db.session.commit()

        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        stats = feats['processor']
        by_name = {s['name']: s for s in stats}
        self.assertAlmostEqual(by_name['CPU A']['mean'], 10.0)
        self.assertEqual(by_name['CPU A']['n'], 1)
        self.assertEqual(by_name['CPU A']['n_runs'], 2)
        self.assertAlmostEqual(by_name['CPU B']['mean'], 20.0)
        self.assertAlmostEqual(by_name['CPU C']['mean'], 30.0)

    # ── edge case: None values are filtered ───────────────────────

    def test_none_value_filtered(self):
        """A system whose only values are None should not appear in feature stats."""
        sys = self._system('sys-a', 'Processor: CPU A', 'OS: Linux')
        sys_b = self._system('sys-b', 'Processor: CPU B', 'OS: Linux')
        sys_c = self._system('sys-c', 'Processor: CPU C', 'OS: Linux')
        bm = self._benchmark('pts/bench-1.0.0', 'Bench')
        self._result(sys, bm, 10.0)
        self._result(sys_b, bm, 20.0)
        r = BenchmarkResult(
            system_id=sys_c.id, benchmark_id=bm.id, value=None
        )
        db.session.add(r)
        db.session.commit()

        analyze_benchmarks(incremental=False)
        a = BenchmarkAnalysis.query.first()
        feats = self._features(a.analysis_json)
        # Filter out error entries (insufficient data) and collect names
        names = set()
        for stats in feats.values():
            for entry in stats:
                if 'error' not in entry:
                    names.add(entry.get('name', ''))
        self.assertNotIn('CPU C', names)


if __name__ == "__main__":
    unittest.main()
