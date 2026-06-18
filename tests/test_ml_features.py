"""Unit tests for ML feature helpers (no database)."""

import unittest

from app.sensor_quality import _series_slope, _label_bucket


class TestFeatureHelpers(unittest.TestCase):
    def test_series_slope_rising(self):
        slope = _series_slope([60.0, 65.0, 72.0, 78.0])
        self.assertIsNotNone(slope)
        self.assertGreater(slope, 0)

    def test_series_slope_flat(self):
        self.assertIsNone(_series_slope([70.0, 70.0]))

    def test_label_bucket_cpu(self):
        self.assertEqual(_label_bucket("CPU Temperature", "Celsius"), "cpu")

    def test_label_bucket_gpu(self):
        self.assertEqual(_label_bucket("GPU Usage", "%"), "gpu")


if __name__ == "__main__":
    unittest.main()
