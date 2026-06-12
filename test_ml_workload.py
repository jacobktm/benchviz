"""Unit tests for ML workload fingerprint (no database)."""

import unittest

from app.ml.workload import compute_workload_fingerprint


class TestWorkloadFingerprint(unittest.TestCase):
    def test_cache_heavy_from_perf(self):
        perf = {
            "instructions": 1e9,
            "cycles": 2e9,
            "cache_references": 1e8,
            "cache_misses": 5e6,
        }
        wl = compute_workload_fingerprint(perf, {}, title="7-Zip Compression")
        self.assertGreater(wl["proportions"].get("cache", 0), 0.25)
        self.assertIn("cache miss", " ".join(wl["evidence"]))
        self.assertEqual(wl["source"], "perf+sensors")

    def test_gpu_from_usage_sensor(self):
        wl = compute_workload_fingerprint(
            {},
            {"gpu_usage_peak": 85.0, "cpu_usage_peak": 15.0},
            title="Vulkan Ray Tracing",
        )
        self.assertGreater(wl["proportions"].get("gpu", 0), 0.3)
        self.assertIn("gpu", wl.get("active_bottlenecks", []) or wl["scope"])

    def test_thermal_notable_high_temp(self):
        wl = compute_workload_fingerprint(
            {},
            {"cpu_temp_peak": 88.0, "cpu_temp_slope": 0.2},
            title="Stress Test",
        )
        self.assertTrue(wl.get("thermal_notable") or wl["scores"]["thermal"] >= 1.0)

    def test_cpu_temp_not_counted_as_usage(self):
        """Temperature peaks must not inflate CPU usage proportion."""
        wl = compute_workload_fingerprint(
            {},
            {"cpu_temp_peak": 82.0},
            title="Idle-ish",
        )
        props = wl["proportions"]
        self.assertLess(props.get("cpu", 0), 0.5)


if __name__ == "__main__":
    unittest.main()
