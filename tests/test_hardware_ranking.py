"""Tests for hardware-adjusted ranking module (black-box)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app import create_app, db
from app.models import Benchmark, BenchmarkResult, HardwareSpec, System
from app.ml.hardware_ranking import (
    _available_feature_keys,
    _extract_hw_features,
    rank_benchmark,
)


class TestExtractHwFeatures(unittest.TestCase):
    """_extract_hw_features: spec object &rarr; feature dict."""

    def test_none_spec(self):
        self.assertEqual(_extract_hw_features(None), {})

    def test_empty_spec(self):
        mock = MagicMock()
        mock.cpu_cores = None
        mock.cpu_threads = None
        mock.cpu_spec = None
        mock.gpu_spec = None
        mock.memory_spec = None
        self.assertEqual(_extract_hw_features(mock), {})

    def test_flat_columns(self):
        mock = MagicMock()
        mock.cpu_cores = 8
        mock.cpu_threads = 16
        mock.cpu_spec = {}
        mock.gpu_spec = {}
        mock.memory_spec = {}
        feats = _extract_hw_features(mock)
        self.assertEqual(feats.get('cpu_cores'), 8.0)
        self.assertEqual(feats.get('cpu_threads'), 16.0)

    def test_cpu_spec_blob(self):
        mock = MagicMock()
        mock.cpu_cores = None
        mock.cpu_threads = None
        mock.cpu_spec = {'boost_clock_mhz': 5000, 'l3_cache_kb': 65536, 'tdp_watts': 170}
        mock.gpu_spec = {}
        mock.memory_spec = {}
        feats = _extract_hw_features(mock)
        self.assertEqual(feats['cpu_boost_clock_mhz'], 5000.0)
        self.assertEqual(feats['cpu_l3_cache_kb'], 65536.0)
        self.assertEqual(feats['cpu_tdp_watts'], 170.0)

    def test_gpu_spec_blob(self):
        mock = MagicMock()
        mock.cpu_cores = None
        mock.cpu_threads = None
        mock.cpu_spec = {}
        mock.gpu_spec = {'vram_mb': 16384, 'shader_count': 10752}
        mock.memory_spec = {}
        feats = _extract_hw_features(mock)
        self.assertEqual(feats['gpu_vram_mb'], 16384.0)
        self.assertEqual(feats['gpu_shader_count'], 10752.0)

    def test_memory_spec_blob(self):
        mock = MagicMock()
        mock.cpu_cores = None
        mock.cpu_threads = None
        mock.cpu_spec = {}
        mock.gpu_spec = {}
        mock.memory_spec = {'size_mb': 131072, 'speed_mhz': 6000, 'channels': 4}
        feats = _extract_hw_features(mock)
        self.assertEqual(feats['memory_size_mb'], 131072.0)
        self.assertEqual(feats['memory_speed_mhz'], 6000.0)
        self.assertEqual(feats['memory_channels'], 4.0)


class TestAvailableFeatureKeys(unittest.TestCase):
    """_available_feature_keys: per-system feature dicts &rarr; shared key list."""

    def test_returns_keys_present_in_2_plus_systems(self):
        sf = {
            1: {'cpu_cores': 8, 'gpu_vram_mb': 8192, 'memory_size_mb': 65536},
            2: {'cpu_cores': 16, 'gpu_vram_mb': 16384},
            3: {'cpu_cores': 4},
        }
        keys = _available_feature_keys(sf)
        self.assertIn('cpu_cores', keys)
        self.assertIn('gpu_vram_mb', keys)
        self.assertNotIn('memory_size_mb', keys)

    def test_empty_when_no_shared_keys(self):
        sf = {
            1: {'cpu_cores': 8},
            2: {'gpu_vram_mb': 8192},
        }
        self.assertEqual(_available_feature_keys(sf), [])


class _RankBenchmarkDbTest(unittest.TestCase):
    """rank_benchmark: benchmark title + version &rarr; ranking dict.

    Black-box: seed real DB rows, call the function with the title,
    assert the output dict has expected structure and values.
    """

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

    def _seed_system(self, identifier: str, cpu_cores: int | None = None,
                     cpu_threads: int | None = None) -> tuple[System, HardwareSpec]:
        sys = System(identifier=identifier, hardware=identifier, software='OS')
        db.session.add(sys)
        db.session.flush()
        hw = HardwareSpec(system_id=sys.id, cpu_cores=cpu_cores, cpu_threads=cpu_threads)
        db.session.add(hw)
        db.session.flush()
        return sys, hw

    def _seed_benchmark(self, title: str = 'Bench', app_version: str = '1.0',
                        proportion: str = 'HIB', scale: str = 'score') -> Benchmark:
        bm = Benchmark(
            title=title, app_version=app_version, identifier=f'pts/{title}-{app_version}',
            proportion=proportion, scale=scale, display_format='BAR_GRAPH', is_primary=True,
        )
        db.session.add(bm)
        db.session.flush()
        return bm

    def _seed_result(self, benchmark_id: int, system_id: int, value: float,
                     arguments: str = '') -> BenchmarkResult:
        r = BenchmarkResult(benchmark_id=benchmark_id, system_id=system_id,
                            value=value, arguments=arguments)
        db.session.add(r)
        db.session.flush()
        return r


class TestRankBenchmarkNotAvailable(_RankBenchmarkDbTest):
    """rank_benchmark returns available=False when insufficient data."""

    def test_no_results(self):
        bm = self._seed_benchmark()
        db.session.commit()
        result = rank_benchmark('Bench', '1.0')
        self.assertFalse(result['available'])

    def test_fewer_than_3_systems(self):
        bm = self._seed_benchmark()
        s1, _ = self._seed_system('sys1', cpu_cores=4)
        s2, _ = self._seed_system('sys2', cpu_cores=8)
        self._seed_result(bm.id, s1.id, 100)
        self._seed_result(bm.id, s2.id, 200)
        db.session.commit()
        result = rank_benchmark('Bench', '1.0')
        self.assertFalse(result['available'])


class TestRankBenchmarkAvailable(_RankBenchmarkDbTest):
    """rank_benchmark returns available=True with sufficient data."""

    def test_three_identical_systems_have_ratio_near_one(self):
        bm = self._seed_benchmark(proportion='HIB', scale='points')
        specs: list[tuple[str, int, int, float]] = [
            ('sys1', 8, 16, 100),
            ('sys2', 8, 16, 100),
            ('sys3', 8, 16, 100),
        ]
        for ident, cores, threads, score in specs:
            s, _ = self._seed_system(ident, cpu_cores=cores, cpu_threads=threads)
            self._seed_result(bm.id, s.id, score)
        db.session.commit()

        result = rank_benchmark('Bench', '1.0')
        self.assertTrue(result['available'])
        self.assertEqual(result['n_systems'], 3)
        self.assertEqual(len(result['systems']), 3)
        for sys_entry in result['systems']:
            self.assertIsNotNone(sys_entry['ratio'])
            self.assertAlmostEqual(sys_entry['ratio'], 1.0, delta=0.05)
            self.assertIsNotNone(sys_entry['overperformance_pct'])
            self.assertAlmostEqual(sys_entry['overperformance_pct'], 0.0, delta=2.0)

    def test_output_structure_is_correct(self):
        bm = self._seed_benchmark(proportion='LIB', scale='seconds')
        specs: list[tuple[str, int, int, float]] = [
            ('sys1', 4, 4, 50),
            ('sys2', 8, 8, 30),
            ('sys3', 16, 16, 20),
        ]
        for ident, cores, threads, score in specs:
            s, _ = self._seed_system(ident, cpu_cores=cores, cpu_threads=threads)
            self._seed_result(bm.id, s.id, score)
        db.session.commit()

        result = rank_benchmark('Bench', '1.0')
        self.assertTrue(result['available'])
        self.assertEqual(result['benchmark_title'], 'Bench')
        self.assertEqual(result['app_version'], '1.0')
        self.assertEqual(result['n_systems'], 3)
        self.assertEqual(result['is_lower_better'], True)
        self.assertEqual(result['score_unit'], 'seconds')
        self.assertIn('r2_score', result)
        self.assertIn('feature_keys', result)
        self.assertIn('cpu_cores', result['feature_keys'])
        self.assertIn('alpha', result)

        for sys_entry in result['systems']:
            self.assertIn('system_id', sys_entry)
            self.assertIn('actual_score', sys_entry)
            self.assertIn('expected_score', sys_entry)
            self.assertIn('ratio', sys_entry)
            self.assertIn('overperformance_pct', sys_entry)

    def test_systems_ranked_by_overperformance_descending(self):
        bm = self._seed_benchmark(proportion='HIB', scale='fps')
        specs: list[tuple[str, int, int, float]] = [
            ('fast', 16, 32, 400),
            ('medium', 8, 16, 200),
            ('slow', 4, 4, 50),
        ]
        for ident, cores, threads, score in specs:
            s, _ = self._seed_system(ident, cpu_cores=cores, cpu_threads=threads)
            self._seed_result(bm.id, s.id, score)
        db.session.commit()

        result = rank_benchmark('Bench', '1.0')
        self.assertTrue(result['available'])
        overperformances = [s['overperformance'] for s in result['systems']]
        self.assertEqual(overperformances, sorted(overperformances, reverse=True))


if __name__ == '__main__':
    unittest.main()
