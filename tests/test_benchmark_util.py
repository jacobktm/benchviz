"""Tests for benchmark lookup, creation, and orphan cleanup."""

import unittest

from app import create_app, db
from app.models import Benchmark, BenchmarkResult, System
from app.benchmark_util import (
    _norm,
    find_benchmark_definition,
    get_or_create_benchmark,
    delete_orphan_benchmarks,
    delete_system_benchmark_suite,
)


class NormTest(unittest.TestCase):
    """_norm — internal string normalizer."""

    def test_norm_none_returns_empty(self):
        self.assertEqual(_norm(None), '')

    def test_norm_empty_stays_empty(self):
        self.assertEqual(_norm(''), '')

    def test_norm_strips_whitespace(self):
        self.assertEqual(_norm('  hello  '), 'hello')

    def test_norm_preserves_normal_string(self):
        self.assertEqual(_norm('hello'), 'hello')


class BenchmarkUtilDbTest(unittest.TestCase):
    """Tests that exercise the database."""

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

    def _seed_benchmark(self, identifier='pts/test-1.0.0', title='Test',
                        app_version='1.0', description='desc', scale='Seconds',
                        proportion='LIB', display_format='BAR_GRAPH',
                        is_primary=True) -> Benchmark:
        bm = Benchmark(
            identifier=identifier,
            title=title,
            app_version=app_version,
            description=description,
            scale=scale,
            proportion=proportion,
            display_format=display_format,
            is_primary=is_primary,
        )
        db.session.add(bm)
        db.session.flush()
        return bm

    def _seed_system(self, identifier='sys-1') -> System:
        sys = System(identifier=identifier, hardware='CPU', software='OS',
                     user='tester', timestamp='2026-01-01')
        db.session.add(sys)
        db.session.flush()
        return sys

    def _seed_result(self, system: System, benchmark: Benchmark,
                     arguments='default', value=42.0) -> BenchmarkResult:
        r = BenchmarkResult(
            system_id=system.id,
            benchmark_id=benchmark.id,
            arguments=arguments,
            value=value,
        )
        db.session.add(r)
        db.session.flush()
        return r

    # ── find_benchmark_definition ──────────────────────────────────

    def test_find_exact_match(self):
        self._seed_benchmark()
        bm = find_benchmark_definition(
            'pts/test-1.0.0', 'Test', '1.0', 'desc', 'Seconds'
        )
        self.assertIsNotNone(bm)
        self.assertEqual(bm.title, 'Test')

    def test_find_no_match(self):
        self._seed_benchmark()
        bm = find_benchmark_definition(
            'pts/other', 'Other', '2.0', 'other', 'MB'
        )
        self.assertIsNone(bm)

    def test_find_with_none_fields(self):
        """Searching for a row where identifier/app_version/etc are stored as NULL."""
        bm = Benchmark(
            identifier=None,
            title='NoId',
            app_version=None,
            description=None,
            scale=None,
            proportion='LIB',
            display_format='BAR_GRAPH',
            is_primary=True,
        )
        db.session.add(bm)
        db.session.flush()

        found = find_benchmark_definition(None, 'NoId', None, None, None)
        self.assertIsNotNone(found)
        self.assertEqual(found.title, 'NoId')

    def test_find_whitespace_normalized(self):
        self._seed_benchmark()
        bm = find_benchmark_definition(
            '  pts/test-1.0.0  ', '  Test  ', '  1.0  ', '  desc  ', '  Seconds  '
        )
        self.assertIsNotNone(bm)

    # ── get_or_create_benchmark — create ───────────────────────────

    def test_get_or_create_new(self):
        bm = get_or_create_benchmark(
            'pts/new-1.0.0', 'New', '1.0', 'desc', 'Seconds',
            'LIB', 'BAR_GRAPH', True,
        )
        self.assertIsNotNone(bm.id)
        self.assertEqual(bm.title, 'New')
        self.assertEqual(bm.proportion, 'LIB')
        self.assertTrue(bm.is_primary)
        self.assertEqual(Benchmark.query.count(), 1)

    def test_get_or_create_new_with_none_identifier(self):
        bm = get_or_create_benchmark(
            None, 'NoIdent', '1.0', 'desc', 'MB',
            'HIB', 'BAR_GRAPH', True,
        )
        self.assertIsNotNone(bm.id)
        self.assertIsNone(bm.identifier)

    def test_get_or_create_new_flushes_to_get_id(self):
        bm = get_or_create_benchmark(
            'pts/flush-1.0.0', 'Flush', '1.0', 'desc', 'Seconds',
            'LIB', 'BAR_GRAPH', True,
        )
        # id should be set due to flush()
        self.assertIsNotNone(bm.id)

    # ── get_or_create_benchmark — existing ────────────────────────

    def test_get_or_create_existing_returns_same(self):
        bm1 = get_or_create_benchmark(
            'pts/existing-1.0.0', 'Existing', '1.0', 'desc', 'Seconds',
            'LIB', 'BAR_GRAPH', True,
        )
        bm2 = get_or_create_benchmark(
            'pts/existing-1.0.0', 'Existing', '1.0', 'desc', 'Seconds',
            'HIB', 'LINE_GRAPH', False,  # different values
        )
        self.assertEqual(bm1.id, bm2.id)
        self.assertEqual(Benchmark.query.count(), 1)

    def test_get_or_create_existing_updates_fields(self):
        bm = get_or_create_benchmark(
            'pts/update-1.0.0', 'Update', '1.0', 'desc', 'Seconds',
            'LIB', 'BAR_GRAPH', True,
        )
        # Re-fetch with updated fields
        bm2 = get_or_create_benchmark(
            'pts/update-1.0.0', 'Update', '1.0', 'desc', 'Seconds',
            'HIB', 'LINE_GRAPH', False,
        )
        self.assertEqual(bm2.proportion, 'HIB')
        self.assertEqual(bm2.display_format, 'LINE_GRAPH')
        self.assertFalse(bm2.is_primary)

    # ── delete_orphan_benchmarks ───────────────────────────────────

    def test_delete_orphans_removes_benchmark_without_results(self):
        self._seed_benchmark()
        self.assertEqual(Benchmark.query.count(), 1)
        deleted = delete_orphan_benchmarks()
        self.assertEqual(deleted, 1)
        self.assertEqual(Benchmark.query.count(), 0)

    def test_delete_orphans_keeps_benchmark_with_results(self):
        bm = self._seed_benchmark()
        sys = self._seed_system()
        self._seed_result(sys, bm)
        deleted = delete_orphan_benchmarks()
        self.assertEqual(deleted, 0)
        self.assertEqual(Benchmark.query.count(), 1)

    def test_delete_orphans_only_removes_orphans(self):
        bm1 = self._seed_benchmark(identifier='pts/a-1.0.0', title='A')
        bm2 = self._seed_benchmark(identifier='pts/b-1.0.0', title='B')
        sys = self._seed_system()
        self._seed_result(sys, bm1)
        deleted = delete_orphan_benchmarks()
        self.assertEqual(deleted, 1)
        titles = {b.title for b in Benchmark.query.all()}
        self.assertEqual(titles, {'A'})

    def test_delete_orphans_no_orphans_returns_zero(self):
        bm = self._seed_benchmark()
        sys = self._seed_system()
        self._seed_result(sys, bm)
        self.assertEqual(delete_orphan_benchmarks(), 0)

    def test_delete_orphans_empty_table_returns_zero(self):
        self.assertEqual(delete_orphan_benchmarks(), 0)

    # ── delete_system_benchmark_suite ──────────────────────────────

    def _call_delete_suite(self, system_id, title, app_version='1.0',
                           identifier=None):
        """Convenience: include the test identifier so match works."""
        if identifier is None:
            identifier = 'pts/test-1.0.0'
        return delete_system_benchmark_suite(
            system_id, title, app_version, identifier=identifier
        )

    def test_delete_suite_removes_results(self):
        sys = self._seed_system()
        bm = self._seed_benchmark()
        self._seed_result(sys, bm)
        deleted = self._call_delete_suite(sys.id, 'Test')
        self.assertEqual(deleted, 1)
        self.assertEqual(BenchmarkResult.query.count(), 0)

    def test_delete_suite_removes_orphan_benchmark(self):
        """After deleting the only result, the benchmark should also be removed."""
        sys = self._seed_system()
        bm = self._seed_benchmark()
        self._seed_result(sys, bm)
        self._call_delete_suite(sys.id, 'Test')
        self.assertEqual(Benchmark.query.count(), 0)

    def test_delete_suite_keeps_other_results(self):
        sys = self._seed_system()
        bm1 = self._seed_benchmark(identifier='pts/a-1.0.0', title='A')
        bm2 = self._seed_benchmark(identifier='pts/b-1.0.0', title='B')
        self._seed_result(sys, bm1, arguments='a')
        self._seed_result(sys, bm2, arguments='b')
        deleted = self._call_delete_suite(sys.id, 'A', identifier='pts/a-1.0.0')
        self.assertEqual(deleted, 1)
        self.assertEqual(BenchmarkResult.query.count(), 1)
        self.assertEqual(BenchmarkResult.query.first().arguments, 'b')

    def test_delete_suite_no_matching_title_returns_zero(self):
        sys = self._seed_system()
        bm = self._seed_benchmark()
        self._seed_result(sys, bm)
        deleted = self._call_delete_suite(sys.id, 'NonExistent')
        self.assertEqual(deleted, 0)
        self.assertEqual(BenchmarkResult.query.count(), 1)

    def test_delete_suite_no_results_for_system_returns_zero(self):
        sys = self._seed_system()
        bm = self._seed_benchmark()
        self._seed_result(sys, bm)
        other_sys = self._seed_system(identifier='other')
        deleted = self._call_delete_suite(other_sys.id, 'Test')
        self.assertEqual(deleted, 0)

    def test_delete_suite_with_explicit_identifier(self):
        sys = self._seed_system()
        bm = self._seed_benchmark(identifier='pts/with-id-1.0.0')
        self._seed_result(sys, bm)
        deleted = delete_system_benchmark_suite(
            sys.id, 'Test', '1.0', identifier='pts/with-id-1.0.0'
        )
        self.assertEqual(deleted, 1)

    def test_delete_suite_with_null_identifier_in_db(self):
        """Match a benchmark where the identifier column is NULL."""
        sys = self._seed_system()
        bm = self._seed_benchmark(identifier=None)
        self._seed_result(sys, bm)
        deleted = delete_system_benchmark_suite(
            sys.id, 'Test', '1.0', identifier=None
        )
        self.assertEqual(deleted, 1)

    def test_delete_suite_identifier_mismatch_returns_zero(self):
        sys = self._seed_system()
        bm = self._seed_benchmark(identifier='pts/real-1.0.0')
        self._seed_result(sys, bm)
        deleted = delete_system_benchmark_suite(
            sys.id, 'Test', '1.0', identifier='pts/wrong-1.0.0'
        )
        self.assertEqual(deleted, 0)

    def test_delete_suite_app_version_none_matches_none_in_db(self):
        """app_version=None in DB, app_version=None in call should match."""
        sys = self._seed_system()
        bm = self._seed_benchmark(identifier=None, app_version=None)
        self._seed_result(sys, bm)
        deleted = delete_system_benchmark_suite(
            sys.id, 'Test', None, identifier=None
        )
        self.assertEqual(deleted, 1)

    def test_delete_suite_app_version_filter_excludes_wrong_version(self):
        """A benchmark with app_version='1.0' does not match app_version='2.0'."""
        sys = self._seed_system()
        bm = self._seed_benchmark(app_version='1.0')
        self._seed_result(sys, bm)
        deleted = self._call_delete_suite(sys.id, 'Test', app_version='2.0')
        self.assertEqual(deleted, 0)

    def test_delete_suite_mixed_suite_titles(self):
        """Only results with the matching title are deleted."""
        sys = self._seed_system()
        bm_a = self._seed_benchmark(identifier='pts/a-1.0.0', title='SuiteA')
        bm_b = self._seed_benchmark(identifier='pts/b-1.0.0', title='SuiteB')
        self._seed_result(sys, bm_a, arguments='a')
        self._seed_result(sys, bm_b, arguments='b')
        deleted = self._call_delete_suite(sys.id, 'SuiteA', identifier='pts/a-1.0.0')
        self.assertEqual(deleted, 1)
        remaining = BenchmarkResult.query.all()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].arguments, 'b')


if __name__ == "__main__":
    unittest.main()
