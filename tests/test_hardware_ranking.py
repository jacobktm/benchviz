"""Tests for hardware-adjusted ranking module."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.hardware_spec import auto_populate_hardware_spec
from app.ml.hardware_ranking import _available_feature_keys, _extract_hw_features, rank_benchmark


class TestExtractHwFeatures(unittest.TestCase):
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
        self.assertEqual(feats.get('cpu_boost_clock_mhz'), 5000.0)
        self.assertEqual(feats.get('cpu_l3_cache_kb'), 65536.0)
        self.assertEqual(feats.get('cpu_tdp_watts'), 170.0)

    def test_gpu_spec_blob(self):
        mock = MagicMock()
        mock.cpu_cores = None
        mock.cpu_threads = None
        mock.cpu_spec = {}
        mock.gpu_spec = {'vram_mb': 16384, 'shader_count': 10752}
        mock.memory_spec = {}
        feats = _extract_hw_features(mock)
        self.assertEqual(feats.get('gpu_vram_mb'), 16384.0)
        self.assertEqual(feats.get('gpu_shader_count'), 10752.0)

    def test_memory_spec_blob(self):
        mock = MagicMock()
        mock.cpu_cores = None
        mock.cpu_threads = None
        mock.cpu_spec = {}
        mock.gpu_spec = {}
        mock.memory_spec = {'size_mb': 131072, 'speed_mhz': 6000, 'channels': 4}
        feats = _extract_hw_features(mock)
        self.assertEqual(feats.get('memory_size_mb'), 131072.0)
        self.assertEqual(feats.get('memory_speed_mhz'), 6000.0)
        self.assertEqual(feats.get('memory_channels'), 4.0)


class TestAvailableFeatureKeys(unittest.TestCase):
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


class TestRankBenchmark(unittest.TestCase):
    def test_not_available_with_few_systems(self):
        with patch('app.ml.hardware_ranking.BenchmarkRepository') as mock_repo:
            mock_bm = MagicMock()
            mock_bm.proportion = 'HIB'
            mock_bm.scale = 'seconds'
            mock_repo.find_primary_by_title.return_value = [mock_bm]

            with patch('app.ml.hardware_ranking.BenchmarkResult') as mock_br:
                mock_br.query.filter.return_value.filter.return_value.filter.return_value.all.return_value = []
                result = rank_benchmark('test', '1.0')
                self.assertFalse(result['available'])
                self.assertIn('3 systems', result['reason'])


if __name__ == '__main__':
    unittest.main()
