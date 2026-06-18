"""Insights workload routing prefers ML profile over legacy cohort heuristics."""

import unittest

from app.workload_profile import (
    _ml_workload_context_from_analysis,
    workload_context_for_insights,
)


class TestInsightsMlScope(unittest.TestCase):
    def test_ml_gpu_profile_routes_insights(self):
        analysis_json = {
            "_ml_profile": {
                "by_args": {
                    "default": {
                        "workload": {
                            "scope": "gpu",
                            "active_bottlenecks": ["gpu"],
                            "proportions": {"gpu": 1.0, "cpu": 0.0},
                            "confidence": 0.88,
                            "source": "sensors+hardware_baseline",
                        },
                    },
                },
            },
            "_workload_by_args": {
                "default": {"scope": "cpu", "active_bottlenecks": ["cpu"]},
            },
        }
        ctx = workload_context_for_insights(
            "AOM AV1", "2019-02-11", "default", analysis_json, "aom av1",
        )
        self.assertEqual(ctx["scope"], "gpu")
        self.assertEqual(ctx["source"], "ml_profile")
        self.assertEqual(ctx["active_bottlenecks"], ["gpu"])

    def test_insufficient_ml_falls_back_to_legacy(self):
        analysis_json = {
            "_ml_profile": {
                "by_args": {
                    "default": {
                        "workload": {
                            "scope": "general",
                            "insufficient_signal": True,
                        },
                    },
                },
            },
            "_workload_by_args": {
                "default": {
                    "scope": "cpu",
                    "active_bottlenecks": ["cpu"],
                    "score_proportions": {"cpu": 1.0},
                },
            },
        }
        ctx = workload_context_for_insights(
            "7-Zip", "", "default", analysis_json, "7-zip compression",
        )
        self.assertEqual(ctx["scope"], "cpu")
        self.assertEqual(ctx["source"], "legacy_profile")

    def test_mixed_bottlenecks_union_hardware(self):
        wl = {
            "scope": "mixed",
            "active_bottlenecks": ["cpu", "gpu"],
            "proportions": {"cpu": 0.55, "gpu": 0.45},
            "confidence": 0.7,
        }
        ctx = _ml_workload_context_from_analysis(
            {"_ml_profile": {"by_args": {"default": {"workload": wl}}}},
            "default",
        )
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx["scope"], "mixed")
        self.assertEqual(set(ctx["active_bottlenecks"]), {"cpu", "gpu"})


if __name__ == "__main__":
    unittest.main()
