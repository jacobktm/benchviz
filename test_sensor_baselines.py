"""Unit tests for per-hardware sensor baseline normalization (no database)."""

import unittest

from app.ml.sensor_baselines import HardwareSensorBaselineIndex, SensorRangeBaseline


class TestSensorRangeBaseline(unittest.TestCase):
    def test_load_fraction_at_idle_and_load(self):
        base = SensorRangeBaseline(
            hardware_part="processor",
            match_key="7800x3d",
            signal_key="cpu.temp_peak",
            n_samples=20,
            idle=45.0,
            load=85.0,
            span=40.0,
            median=62.0,
        )
        self.assertAlmostEqual(base.load_fraction(45.0), 0.0)
        self.assertAlmostEqual(base.load_fraction(85.0), 1.0)
        self.assertAlmostEqual(base.load_fraction(65.0), 0.5)

    def test_load_fraction_clamped(self):
        base = SensorRangeBaseline(
            hardware_part="processor",
            match_key="7800x3d",
            signal_key="cpu.usage_peak",
            n_samples=10,
            idle=10.0,
            load=90.0,
            span=80.0,
            median=55.0,
        )
        self.assertAlmostEqual(base.load_fraction(5.0), 0.0)
        self.assertAlmostEqual(base.load_fraction(150.0), 1.75)


class TestHardwareSensorBaselineIndex(unittest.TestCase):
    def test_model_specific_before_global(self):
        index = HardwareSensorBaselineIndex()
        index.baselines[("processor", "7800x3d", "cpu.temp_peak")] = SensorRangeBaseline(
            hardware_part="processor",
            match_key="7800x3d",
            signal_key="cpu.temp_peak",
            n_samples=10,
            idle=40.0,
            load=70.0,
            span=30.0,
            median=55.0,
        )
        index.baselines[("processor", "__global__", "cpu.temp_peak")] = SensorRangeBaseline(
            hardware_part="processor",
            match_key="__global__",
            signal_key="cpu.temp_peak",
            n_samples=100,
            idle=35.0,
            load=90.0,
            span=55.0,
            median=60.0,
        )
        frac = index.normalize("processor", "7800x3d", "cpu.temp_peak", 55.0)
        self.assertAlmostEqual(frac, 0.5)

    def test_global_fallback(self):
        index = HardwareSensorBaselineIndex()
        index.baselines[("processor", "__global__", "cpu.usage_peak")] = SensorRangeBaseline(
            hardware_part="processor",
            match_key="__global__",
            signal_key="cpu.usage_peak",
            n_samples=50,
            idle=5.0,
            load=95.0,
            span=90.0,
            median=40.0,
        )
        frac = index.normalize("processor", "unknown_cpu", "cpu.usage_peak", 50.0)
        self.assertAlmostEqual(frac, 0.5)


if __name__ == "__main__":
    unittest.main()
