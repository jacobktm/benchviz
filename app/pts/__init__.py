"""Phoronix Test Suite comparison and scoring package."""

from .hashing import (
    COMPOSITE_OPTION_CAP_RATIO,
    _is_hib,
    comparison_hash_for_benchmark,
    generate_comparison_hash,
    hash_identifier_from_test_profile,
    normalize_ob_unit,
    parse_version_tuple,
    proportion_is_lower_better,
    strip_test_profile_identifier,
    test_profile_family,
)
from .math_aggregation import (
    MIN_HARMONIC_SUBTESTS,
    is_harmonic_mean_scale,
    normalize_harmonic_scale_key,
    pts_geometric_mean_composite,
    pts_geometric_mean_ob_composite,
    pts_harmonic_mean_by_scale,
    pts_harmonic_mean_cross_scale,
)
from .ob_baselines import (
    capped_relative_score,
    lib_to_hib_value,
    normalize_relative_values,
    ob_median_from_entry,
    ob_p1_from_entry,
    ob_percentile_value_from_entry,
    ob_percentiles_for_systems,
    relative_vs_ob_baseline,
    relative_vs_ob_median,
)

_COMPARE_SYMBOLS = {
    "build_pts_context_for_compare_group",
    "build_pts_global_harmonic_summary",
    "build_pts_global_summary",
    "build_pts_ob_global_summary",
}


def __getattr__(name):
    if name in _COMPARE_SYMBOLS:
        from .compare import (
            build_pts_context_for_compare_group,
            build_pts_global_harmonic_summary,
            build_pts_global_summary,
            build_pts_ob_global_summary,
        )
        return locals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "COMPOSITE_OPTION_CAP_RATIO",
    "MIN_HARMONIC_SUBTESTS",
    "_is_hib",
    "build_pts_context_for_compare_group",
    "build_pts_global_harmonic_summary",
    "build_pts_global_summary",
    "build_pts_ob_global_summary",
    "capped_relative_score",
    "comparison_hash_for_benchmark",
    "generate_comparison_hash",
    "hash_identifier_from_test_profile",
    "is_harmonic_mean_scale",
    "lib_to_hib_value",
    "normalize_harmonic_scale_key",
    "normalize_ob_unit",
    "normalize_relative_values",
    "ob_median_from_entry",
    "ob_p1_from_entry",
    "ob_percentile_value_from_entry",
    "ob_percentiles_for_systems",
    "parse_version_tuple",
    "proportion_is_lower_better",
    "pts_geometric_mean_composite",
    "pts_geometric_mean_ob_composite",
    "pts_harmonic_mean_by_scale",
    "pts_harmonic_mean_cross_scale",
    "relative_vs_ob_baseline",
    "relative_vs_ob_median",
    "strip_test_profile_identifier",
    "test_profile_family",
]
