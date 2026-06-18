import importlib.util
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_consensus_spec = importlib.util.spec_from_file_location(
    "workload_consensus",
    os.path.join(_ROOT, "app", "workload_consensus.py"),
)
_consensus_mod = importlib.util.module_from_spec(_consensus_spec)
assert _consensus_spec.loader is not None
_consensus_spec.loader.exec_module(_consensus_mod)
scope_consensus = _consensus_mod.scope_consensus
classification_from_cohort_consensus = _consensus_mod.classification_from_cohort_consensus
resolve_cohort_scope = _consensus_mod.resolve_cohort_scope

def option_profile_key(description, scale=None):
    desc = (description or "").strip() or "primary"
    sc = (scale or "").strip()
    return f"{desc}|{sc}" if sc else desc


def _result_matches_option(result_args, config_args, option_description="", option_scale=""):
    import re

    def norm(*parts):
        return " ".join((p or "").strip().lower() for p in parts if p)

    ra = (result_args or "").strip()
    ca = (config_args or "").strip()
    if not (option_description or option_scale):
        return True
    if ra == ca:
        matched = True
    elif not ca:
        matched = not ra
    else:
        matched = ra.endswith(ca) or ca in ra
    if not matched:
        return False
    suffix = ""
    if ra:
        if ca and ra.endswith(ca) and ra != ca:
            suffix = ra[: -len(ca)].strip()
    if not suffix:
        return True
    blob = norm(option_description, option_scale)
    suf = norm(suffix)
    if not blob:
        return True
    if suf in blob or blob in suf:
        return True
    opt_tokens = {t for t in re.findall(r"[a-z0-9]+", blob) if len(t) >= 3}
    suf_tokens = {t for t in re.findall(r"[a-z0-9]+", suf) if len(t) >= 3}
    return bool(opt_tokens & suf_tokens)


class WorkloadConsensusTest(unittest.TestCase):
    def test_scope_consensus_requires_agreement(self):
        stable, dom, agr = scope_consensus(["gpu", "gpu", "cpu"])
        self.assertTrue(stable)
        self.assertEqual(dom, "gpu")
        self.assertAlmostEqual(agr, 2 / 3)

    def test_scope_vote_split_is_informational_only(self):
        stable, dom, agr = scope_consensus(["gpu", "cpu"])
        self.assertFalse(stable)
        self.assertEqual(dom, "gpu")
        self.assertAlmostEqual(agr, 0.5)

    def test_equal_cpu_gpu_scores_are_mixed_workload(self):
        scope, taxonomy, active = resolve_cohort_scope(
            {"cpu": 3.0, "memory": 0.2, "gpu": 3.0, "storage": 0.1},
            ["cpu", "gpu"],
        )
        self.assertEqual(scope, "mixed")
        self.assertEqual(taxonomy, "mixed_workload")
        self.assertEqual(set(active), {"cpu", "gpu"})

    def test_cohort_imputation_allows_fifty_fifty_split(self):
        out = classification_from_cohort_consensus(
            {"cpu": 3.0, "memory": 0.2, "gpu": 3.0, "storage": 0.1},
            ["cpu", "gpu"],
            n_with_evidence=2,
            n_imputed=1,
            title_blob="",
        )
        self.assertEqual(out["scope"], "mixed")
        self.assertEqual(out["taxonomy"], "mixed_workload")
        self.assertEqual(out["source"], "cohort_imputed")
        props = out["score_proportions"]
        self.assertAlmostEqual(props["cpu"], props["gpu"], places=2)
        self.assertGreater(props["cpu"], 0.4)
        self.assertEqual(set(out["active_bottlenecks"]), {"cpu", "gpu"})

    def test_cohort_imputation_carries_proportions(self):
        out = classification_from_cohort_consensus(
            {"cpu": 1.0, "memory": 0.5, "gpu": 4.0, "storage": 0.2},
            ["gpu", "gpu", "gpu"],
            n_with_evidence=3,
            n_imputed=2,
            title_blob="",
        )
        self.assertEqual(out["scope"], "gpu")
        self.assertEqual(out["source"], "cohort_imputed")
        props = out["score_proportions"]
        self.assertGreater(props["gpu"], props["cpu"])


class WorkloadOptionTest(unittest.TestCase):
    def test_option_profile_key(self):
        self.assertEqual(option_profile_key("Compression", "MIPS"), "Compression|MIPS")

    def test_shared_config_results_match_any_option(self):
        self.assertTrue(_result_matches_option(
            "-width 1920 -height 1080",
            "-width 1920 -height 1080",
            "WebGL",
            "FPS",
        ))

    def test_option_suffix_filters_to_matching_option(self):
        self.assertTrue(_result_matches_option(
            "WebGL -width 1920 -height 1080",
            "-width 1920 -height 1080",
            "WebGL",
            "FPS",
        ))
        self.assertFalse(_result_matches_option(
            "CanvasMark -width 1920 -height 1080",
            "-width 1920 -height 1080",
            "WebGL",
            "FPS",
        ))


if __name__ == "__main__":
    unittest.main()
