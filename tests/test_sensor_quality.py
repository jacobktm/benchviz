import importlib.util
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "sensor_quality",
    os.path.join(_ROOT, "app", "sensor_quality.py"),
)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)
chart_has_usable_signal = _mod.chart_has_usable_signal
is_noisy_sensor_series = _mod.is_noisy_sensor_series
series_quality = _mod.series_quality


class SensorQualityTest(unittest.TestCase):
    def test_flat_temperature_is_noisy(self):
        flat = [42.0] * 20
        self.assertTrue(is_noisy_sensor_series(flat, "CPU Temperature", "Celsius"))

    def test_varying_temperature_is_signal(self):
        vals = [40 + (i % 5) * 0.5 for i in range(30)]
        self.assertFalse(is_noisy_sensor_series(vals, "CPU Temperature", "Celsius"))

    def test_idle_gpu_usage_is_noisy(self):
        self.assertTrue(is_noisy_sensor_series([0, 0, 0, 0, 0], "GPU Usage", "%"))

    def test_chart_kept_when_systems_diverge(self):
        traces = [
            {"y": [50.0] * 10},
            {"y": [55.0] * 10},
        ]
        ok, reason = chart_has_usable_signal(traces, "CPU Temperature", "C")
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_too_few_samples(self):
        q = series_quality([1.0, 1.0], "CPU Power", "Watts")
        self.assertTrue(q["is_noisy"])
        self.assertEqual(q["reason"], "too_few_samples")


if __name__ == "__main__":
    unittest.main()
