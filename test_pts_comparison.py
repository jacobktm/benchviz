import importlib.util
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

from app.pts_math import geometric_mean, harmonic_mean, result_to_percentile
from app.pts_comparison import (
    generate_comparison_hash,
    is_harmonic_mean_scale,
    normalize_harmonic_scale_key,
    normalize_relative_values,
    pts_harmonic_mean_by_scale,
    relative_vs_ob_median,
)
from app.pts_compare import build_pts_global_harmonic_summary, build_pts_global_summary


class PtsComparisonHashTest(unittest.TestCase):
    def test_7zip_compression_hash_matches_ob_cache(self):
        h = generate_comparison_hash(
            "pts/compress-7zip-1.10",
            "",
            "Test: Compression Rating",
            "22.01",
            "MIPS",
        )
        self.assertEqual(h, "1ef13e3cbacb2cbf5f2b35f6e4f037552c8625b0")

    def test_normalize_relative_best_is_one(self):
        rel = normalize_relative_values(
            {"a": 100.0, "b": 110.0},
            hib=True,
        )
        self.assertAlmostEqual(rel["a"], 1.0)
        self.assertAlmostEqual(rel["b"], 1.1)

    def test_percentile_top_result(self):
        percentiles = list(range(100, 1100, 10))
        p = result_to_percentile(2000, percentiles, hib=True)
        self.assertEqual(p, 100)


class PtsGlobalSummaryTest(unittest.TestCase):
    def _groups(self, subtests):
        return [{
            "charts": [{
                "is_primary": True,
                "scale": st.get("scale") or "",
                "proportion": "HIB" if st.get("hib", True) else "LIB",
                "traces": [
                    {"name": sid, "y": [val]}
                    for sid, val in (st.get("values") or {}).items()
                    if val is not None
                ],
            }],
        } for st in subtests]

    def test_global_geo_mean_from_raw_subtests(self):
        groups = self._groups([
            {"hib": True, "values": {"a": 100.0, "b": 110.0}},
            {"hib": True, "values": {"a": 200.0, "b": 180.0}},
        ])
        summary = build_pts_global_summary(groups, ["a", "b"])
        self.assertAlmostEqual(summary["composite_raw"]["a"], geometric_mean([100.0, 200.0]))
        self.assertAlmostEqual(summary["composite_raw"]["b"], geometric_mean([110.0, 180.0]))
        self.assertEqual(summary["reference_system_id"], "b")
        self.assertAlmostEqual(summary["composite_relative"]["b"], 1.0)
        self.assertGreater(summary["composite_relative"]["a"], 1.0)

    def test_global_skipped_with_one_subtest(self):
        groups = self._groups([{"hib": True, "values": {"a": 100.0, "b": 110.0}}])
        summary = build_pts_global_summary(groups, ["a", "b"])
        self.assertIsNone(summary["composite_raw"]["a"])
        self.assertIsNone(summary["composite_raw"]["b"])


class PtsObRelativeTest(unittest.TestCase):
    def test_relative_vs_ob_median_hib(self):
        rel = relative_vs_ob_median({"a": 73490.0, "b": 85000.0}, hib=True, ob_median=73490.0)
        self.assertAlmostEqual(rel["a"], 1.0)
        self.assertAlmostEqual(rel["b"], 85000.0 / 73490.0, places=4)


class PtsHarmonicMeanTest(unittest.TestCase):
    def test_harmonic_mean_formula(self):
        self.assertAlmostEqual(harmonic_mean([100.0, 200.0]), 133.333333, places=4)

    def test_harmonic_scale_eligibility(self):
        self.assertTrue(is_harmonic_mean_scale("FPS"))
        self.assertTrue(is_harmonic_mean_scale("MB/s"))
        self.assertFalse(is_harmonic_mean_scale("MIPS"))
        self.assertFalse(is_harmonic_mean_scale("Seconds"))

    def test_harmonic_by_scale_requires_four_subtests(self):
        subtests = [
            {"hib": True, "scale": "FPS", "values": {"a": 100.0, "b": 110.0}},
        ] * 3
        self.assertEqual(pts_harmonic_mean_by_scale(subtests, ["a", "b"]), {})

        subtests = [
            {"hib": True, "scale": "FPS", "values": {"a": 100.0, "b": 110.0}},
        ] * 4
        result = pts_harmonic_mean_by_scale(subtests, ["a", "b"])
        self.assertIn("FPS", result)
        self.assertAlmostEqual(result["FPS"]["relative"]["a"], 1.0)
        self.assertGreater(result["FPS"]["relative"]["b"], 1.0)

    def test_harmonic_skips_lib(self):
        subtests = [
            {"hib": False, "scale": "FPS", "values": {"a": 10.0, "b": 12.0}},
        ] * 4
        self.assertEqual(pts_harmonic_mean_by_scale(subtests, ["a", "b"]), {})

    def test_harmonic_normalizes_byte_rate_scales(self):
        self.assertEqual(normalize_harmonic_scale_key("MiB/s"), "MB/s")
        self.assertEqual(normalize_harmonic_scale_key("MB/s"), "MB/s")

    def test_global_harmonic_aggregates_all_benchmark_charts(self):
        def _group(scale, a_val, b_val, a_name="a", b_name="b"):
            return {
                "system_details": [{"short_name": "a"}, {"short_name": "b"}],
                "charts": [{
                    "is_primary": True,
                    "scale": scale,
                    "proportion": "HIB",
                    "traces": [
                        {"name": a_name, "y": [a_val]},
                        {"name": b_name, "y": [b_val]},
                    ],
                }],
            }

        zstd_groups = [
            _group("MB/s", 100.0, 110.0),
            _group("MB/s", 120.0, 130.0),
            _group("MB/s", 140.0, 150.0),
            _group("MB/s", 160.0, 170.0),
        ]
        all_groups_input = zstd_groups + [
            _group("MiB/s", 200.0, 180.0, "a (HIP)", "b (CUDA)"),
        ]
        zstd_only = build_pts_global_harmonic_summary(zstd_groups)
        all_groups = build_pts_global_harmonic_summary(all_groups_input)
        self.assertIn("MB/s", all_groups)
        self.assertIn("MB/s", zstd_only)
        self.assertEqual(all_groups["MB/s"]["subtest_count"], 5)
        self.assertEqual(zstd_only["MB/s"]["subtest_count"], 4)
        self.assertNotAlmostEqual(
            all_groups["MB/s"]["raw"]["a"],
            zstd_only["MB/s"]["raw"]["a"],
        )


class PtsMathTest(unittest.TestCase):
    def test_geometric_mean(self):
        self.assertAlmostEqual(geometric_mean([1.0, 1.21]), 1.1, places=5)


if __name__ == "__main__":
    unittest.main()
