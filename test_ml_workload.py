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
        self.assertEqual(wl["source"], "perf")

    def test_cpu_only_from_idle_gpu_usage(self):
        wl = compute_workload_fingerprint(
            {},
            {
                "cpu_usage_peak": 78.0,
                "gpu_usage_peak": 2.0,
                "has_cpu_usage": True,
                "has_gpu_usage": True,
            },
            title="Aircrack-ng",
        )
        self.assertGreater(wl["proportions"].get("cpu", 0), 0.5)
        self.assertLess(wl["proportions"].get("gpu", 0), 0.05)
        self.assertIn("GPU idle", " ".join(wl["evidence"]))
        self.assertFalse(wl.get("insufficient_signal"))

    def test_gpu_from_normalized_usage(self):
        wl = compute_workload_fingerprint(
            {},
            {
                "gpu_usage_load_frac": 0.85,
                "cpu_usage_load_frac": 0.15,
                "has_cpu_usage": True,
                "has_gpu_usage": True,
            },
            title="Vulkan Ray Tracing",
        )
        self.assertGreater(wl["proportions"].get("gpu", 0), 0.3)
        self.assertTrue(wl.get("hardware_normalized"))

    def test_gpu_from_usage_sensor(self):
        wl = compute_workload_fingerprint(
            {},
            {
                "gpu_usage_peak": 85.0,
                "cpu_usage_peak": 15.0,
                "has_cpu_usage": True,
                "has_gpu_usage": True,
            },
            title="Vulkan Ray Tracing",
        )
        self.assertGreater(wl["proportions"].get("gpu", 0), 0.3)
        self.assertIn("gpu", wl.get("active_bottlenecks", []) or wl["scope"])

    def test_thermal_from_normalized_temp(self):
        wl = compute_workload_fingerprint(
            {},
            {"cpu_temp_load_frac": 0.75, "cpu_temp_slope_frac": 0.5},
            title="Stress Test",
        )
        self.assertTrue(wl.get("thermal_notable") or wl["scores"]["thermal"] >= 1.0)
        ev = " ".join(wl["evidence"]).lower()
        self.assertTrue("model thermal span" in ev or "inferred from temp" in ev)

    def test_thermal_notable_high_temp(self):
        wl = compute_workload_fingerprint(
            {},
            {"cpu_temp_peak": 88.0, "cpu_temp_slope": 0.2},
            title="Stress Test",
        )
        self.assertTrue(wl.get("thermal_notable") or wl["scores"]["thermal"] >= 1.0)

    def test_cpu_load_inferred_from_temp_and_power_proxy(self):
        wl = compute_workload_fingerprint(
            {},
            {
                "cpu_temp_load_frac": 0.62,
                "cpu_power_load_frac": 0.58,
                "gpu_temp_load_frac": 0.08,
                "has_cpu_temp": True,
                "has_cpu_power": True,
                "has_gpu_temp": True,
            },
            title="Aircrack-ng",
        )
        self.assertGreater(wl["proportions"].get("cpu", 0), 0.5)
        self.assertLess(wl["proportions"].get("gpu", 0), 0.05)
        self.assertTrue(wl.get("load_proxy_used"))
        self.assertIn("inferred", " ".join(wl["evidence"]).lower())
        self.assertFalse(wl.get("insufficient_signal"))

    def test_cpu_temp_not_counted_as_usage(self):
        """Temperature peaks must not inflate CPU usage proportion."""
        wl = compute_workload_fingerprint(
            {},
            {"cpu_temp_peak": 82.0},
            title="Idle-ish",
        )
        self.assertTrue(wl.get("insufficient_signal"))
        self.assertEqual(wl["active_bottlenecks"], [])
        self.assertEqual(wl["scope"], "general")

    def test_no_fake_equal_split_without_signal(self):
        wl = compute_workload_fingerprint({}, {}, title="Unknown Benchmark")
        self.assertTrue(wl.get("insufficient_signal"))
        # equal-distribution fallback when no signal — active_bottlenecks stays empty
        self.assertEqual(wl["active_bottlenecks"], [])
        self.assertEqual(wl["scope"], "general")


if __name__ == "__main__":
    unittest.main()
