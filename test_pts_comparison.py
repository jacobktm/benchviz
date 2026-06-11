import importlib.util
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

from app.pts_math import geometric_mean, result_to_percentile
from app.pts_comparison import generate_comparison_hash, normalize_relative_values
from app.pts_compare import build_pts_global_summary


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


class PtsMathTest(unittest.TestCase):
    def test_geometric_mean(self):
        self.assertAlmostEqual(geometric_mean([1.0, 1.21]), 1.1, places=5)


class PtsGlobalSummaryTest(unittest.TestCase):
    def test_global_geo_mean_from_raw_subtests(self):
        ctx = [{
            "subtests": [
                {"hib": True, "values": {"a": 100.0, "b": 110.0}},
                {"hib": True, "values": {"a": 200.0, "b": 180.0}},
            ],
        }]
        summary = build_pts_global_summary(ctx, ["a", "b"])
        self.assertAlmostEqual(summary["composite_raw"]["a"], geometric_mean([100.0, 200.0]))
        self.assertAlmostEqual(summary["composite_raw"]["b"], geometric_mean([110.0, 180.0]))
        self.assertEqual(summary["reference_system_id"], "b")
        self.assertAlmostEqual(summary["composite_relative"]["b"], 1.0)
        self.assertGreater(summary["composite_relative"]["a"], 1.0)

    def test_global_skipped_with_one_subtest(self):
        ctx = [{"subtests": [{"hib": True, "values": {"a": 100.0, "b": 110.0}}]}]
        summary = build_pts_global_summary(ctx, ["a", "b"])
        self.assertIsNone(summary["composite_raw"]["a"])
        self.assertIsNone(summary["composite_raw"]["b"])


if __name__ == "__main__":
    unittest.main()
