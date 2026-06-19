"""Unit tests for ML feature extraction helpers."""

import unittest
from unittest.mock import MagicMock, patch

from app.ml.features import (
    ThermalSensorFeatures,
    UsageSensorFeatures,
    SensorFeatures,
    SystemRunFeatures,
    pool_perf_signals,
    pool_sensor_features,
    _apply_sensor_baselines,
)


class ThermalSensorFeaturesTest(unittest.TestCase):
    def test_defaults_are_none(self):
        t = ThermalSensorFeatures()
        self.assertIsNone(t.cpu_temp_mean)
        self.assertIsNone(t.gpu_freq_peak)

    def test_custom_values(self):
        t = ThermalSensorFeatures(cpu_temp_mean=70.0, gpu_temp_peak=65.0)
        self.assertEqual(t.cpu_temp_mean, 70.0)
        self.assertEqual(t.gpu_temp_peak, 65.0)


class UsageSensorFeaturesTest(unittest.TestCase):
    def test_defaults_are_none(self):
        u = UsageSensorFeatures()
        self.assertIsNone(u.cpu_usage_peak)

    def test_usage_values(self):
        u = UsageSensorFeatures(cpu_usage_peak=90.0, gpu_usage_peak=45.0)
        self.assertEqual(u.cpu_usage_peak, 90.0)
        self.assertEqual(u.gpu_usage_peak, 45.0)


class SensorFeaturesTest(unittest.TestCase):
    def test_default_construction(self):
        s = SensorFeatures()
        self.assertIsInstance(s.thermal, ThermalSensorFeatures)
        self.assertIsInstance(s.usage, UsageSensorFeatures)
        self.assertFalse(s.has_monitor_data)

    def test_with_data(self):
        t = ThermalSensorFeatures(cpu_temp_mean=68.0)
        u = UsageSensorFeatures(cpu_usage_peak=85.0)
        s = SensorFeatures(thermal=t, usage=u, has_monitor_data=True)
        self.assertEqual(s.thermal.cpu_temp_mean, 68.0)
        self.assertEqual(s.usage.cpu_usage_peak, 85.0)
        self.assertTrue(s.has_monitor_data)
        self.assertEqual(s.normalized, {})
        self.assertEqual(s.hardware_match_keys, {})


class SystemRunFeaturesTest(unittest.TestCase):
    def test_basic_construction(self):
        sf = SystemRunFeatures(
            system_id=1,
            title='Bench',
            app_version='1.0',
            config_args='default',
            score_raw=100.0,
            score_normalized=100.0,
            run_count=3,
            run_stdev=2.5,
            run_cv=0.025,
        )
        self.assertEqual(sf.system_id, 1)
        self.assertEqual(sf.title, 'Bench')
        self.assertAlmostEqual(sf.score_raw, 100.0)

    def test_to_dict_includes_all_keys(self):
        sf = SystemRunFeatures(
            system_id=1, title='T', app_version='1.0',
            config_args='default', score_raw=50.0,
            score_normalized=50.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
        )
        d = sf.to_dict()
        self.assertEqual(d['system_id'], 1)
        self.assertEqual(d['title'], 'T')
        self.assertEqual(d['run_count'], 1)
        self.assertIn('perf', d)
        self.assertIn('sensors', d)
        self.assertIn('hardware', d)

    def test_to_dict_sensors_is_dataclass(self):
        sensors = SensorFeatures(has_monitor_data=True)
        sf = SystemRunFeatures(
            system_id=1, title='T', app_version='1.0',
            config_args='default', score_raw=50.0,
            score_normalized=50.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
            sensors=sensors,
        )
        d = sf.to_dict()
        self.assertIsInstance(d['sensors'], dict)
        self.assertTrue(d['sensors']['has_monitor_data'])


class ApplySensorBaselinesTest(unittest.TestCase):
    def test_no_baseline_index_is_noop(self):
        sensors = SensorFeatures(has_monitor_data=True)
        _apply_sensor_baselines(sensors, {"processor": "CPU A"}, None)
        self.assertEqual(sensors.normalized, {})

    def test_baseline_normalizes_usage(self):
        sensors = SensorFeatures(
            usage=UsageSensorFeatures(cpu_usage_peak=80.0),
            has_monitor_data=True,
        )
        mock_index = MagicMock()
        mock_index.normalize.return_value = 0.75

        _apply_sensor_baselines(sensors, {"processor": "CPU A", "graphics": "GPU B"}, mock_index)
        self.assertIn("cpu_usage_load_frac", sensors.normalized)
        self.assertAlmostEqual(sensors.normalized["cpu_usage_load_frac"], 0.75)
        mock_index.normalize.assert_called()

    def test_baseline_sets_hardware_match_keys(self):
        sensors = SensorFeatures()
        mock_index = MagicMock()
        mock_index.normalize.return_value = 0.5
        _apply_sensor_baselines(
            sensors,
            {"processor": "AMD Ryzen 9 9950X", "graphics": "NVIDIA RTX 4090"},
            mock_index,
        )
        self.assertIn("processor", sensors.hardware_match_keys)
        self.assertIn("graphics", sensors.hardware_match_keys)


class PoolPerfSignalsTest(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(pool_perf_signals([]), {})

    def test_pools_single_row(self):
        sf = SystemRunFeatures(
            system_id=1, title='T', app_version='1.0',
            config_args='default', score_raw=50.0,
            score_normalized=50.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
            perf={"cycles": 1000.0, "instructions": 500.0},
        )
        pooled = pool_perf_signals([sf])
        self.assertAlmostEqual(pooled["cycles"], 1000.0)

    def test_pools_multiple_rows_uses_median(self):
        sf1 = SystemRunFeatures(
            system_id=1, title='T', app_version='1.0',
            config_args='default', score_raw=50.0,
            score_normalized=50.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
            perf={"cycles": 1000.0},
        )
        sf2 = SystemRunFeatures(
            system_id=2, title='T', app_version='1.0',
            config_args='default', score_raw=60.0,
            score_normalized=60.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
            perf={"cycles": 2000.0},
        )
        pooled = pool_perf_signals([sf1, sf2])
        self.assertAlmostEqual(pooled["cycles"], 1500.0)


class PoolSensorFeaturesTest(unittest.TestCase):
    def test_empty_returns_zeros_and_falses(self):
        pooled = pool_sensor_features([])
        self.assertIsNone(pooled.get("cpu_usage_peak"))
        self.assertFalse(pooled["has_cpu_usage"])

    def test_pools_single_row_usage(self):
        sf = SystemRunFeatures(
            system_id=1, title='T', app_version='1.0',
            config_args='default', score_raw=50.0,
            score_normalized=50.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
            sensors=SensorFeatures(
                usage=UsageSensorFeatures(cpu_usage_peak=95.0, gpu_usage_peak=80.0),
                has_monitor_data=True,
            ),
        )
        pooled = pool_sensor_features([sf])
        self.assertAlmostEqual(pooled["cpu_usage_peak"], 95.0)
        self.assertAlmostEqual(pooled["gpu_usage_peak"], 80.0)
        self.assertTrue(pooled["has_cpu_usage"])
        self.assertTrue(pooled["has_gpu_usage"])

    def test_pools_thermal_data(self):
        sf = SystemRunFeatures(
            system_id=1, title='T', app_version='1.0',
            config_args='default', score_raw=50.0,
            score_normalized=50.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
            sensors=SensorFeatures(
                thermal=ThermalSensorFeatures(cpu_temp_peak=85.0, gpu_temp_peak=75.0),
                has_monitor_data=True,
            ),
        )
        pooled = pool_sensor_features([sf])
        self.assertAlmostEqual(pooled["cpu_temp_peak"], 85.0)
        self.assertAlmostEqual(pooled["gpu_temp_peak"], 75.0)
        self.assertTrue(pooled["has_cpu_temp"])
        self.assertTrue(pooled["has_gpu_temp"])

    def test_includes_normalized_when_present(self):
        sf = SystemRunFeatures(
            system_id=1, title='T', app_version='1.0',
            config_args='default', score_raw=50.0,
            score_normalized=50.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
            sensors=SensorFeatures(
                has_monitor_data=True,
                normalized={"cpu_usage_load_frac": 0.75},
            ),
        )
        pooled = pool_sensor_features([sf])
        self.assertAlmostEqual(pooled["cpu_usage_load_frac"], 0.75)

    def test_omits_normalized_when_missing(self):
        sf = SystemRunFeatures(
            system_id=1, title='T', app_version='1.0',
            config_args='default', score_raw=50.0,
            score_normalized=50.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
            sensors=SensorFeatures(has_monitor_data=True),
        )
        pooled = pool_sensor_features([sf])
        self.assertNotIn("cpu_usage_load_frac", pooled)

    def test_cpu_freq_droop(self):
        sf = SystemRunFeatures(
            system_id=1, title='T', app_version='1.0',
            config_args='default', score_raw=50.0,
            score_normalized=50.0, run_count=1,
            run_stdev=0.0, run_cv=0.0,
            sensors=SensorFeatures(
                thermal=ThermalSensorFeatures(cpu_freq_peak=5000.0, cpu_freq_min=4800.0),
                has_monitor_data=True,
            ),
        )
        pooled = pool_sensor_features([sf])
        self.assertAlmostEqual(pooled["cpu_freq_droop"], 200.0)


if __name__ == "__main__":
    unittest.main()
