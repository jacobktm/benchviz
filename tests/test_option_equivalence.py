"""Tests for option equivalence helpers and native resolution extraction."""

import unittest
from types import SimpleNamespace

from app.option_equivalence import (
    resolution_pool_key,
    _resolution_class_key,
    _canonicalize_args_for_pool,
)
from app.components import get_system_components


class ResolutionPoolKeyTest(unittest.TestCase):

    def test_1920x1080_is_1080p_ish(self):
        self.assertEqual(
            resolution_pool_key("1920x1080"),
            "Resolution class: 1080p-ish",
        )

    def test_1920x1200_is_1080p_ish(self):
        """16:10 variant of 1080p groups with 16:9."""
        self.assertEqual(
            resolution_pool_key("1920x1200"),
            "Resolution class: 1080p-ish",
        )

    def test_2560x1440_is_1440p_ish(self):
        self.assertEqual(
            resolution_pool_key("2560x1440"),
            "Resolution class: 1440p-ish",
        )

    def test_2560x1600_is_1440p_ish(self):
        """16:10 variant of 1440p groups with 16:9."""
        self.assertEqual(
            resolution_pool_key("2560x1600"),
            "Resolution class: 1440p-ish",
        )

    def test_3840x2160_is_4k(self):
        self.assertEqual(
            resolution_pool_key("3840x2160"),
            "Resolution class: 4k",
        )

    def test_3840x2400_is_4k(self):
        """16:10 variant of 4k groups with 16:9."""
        self.assertEqual(
            resolution_pool_key("3840x2400"),
            "Resolution class: 4k",
        )

    def test_4k_shorthand(self):
        self.assertEqual(
            resolution_pool_key("4k"),
            "Resolution class: 4k",
        )

    def test_1080p_shorthand(self):
        self.assertEqual(
            resolution_pool_key("1080p"),
            "Resolution class: 1080p",
        )

    def test_1440p_shorthand(self):
        self.assertEqual(
            resolution_pool_key("1440p"),
            "Resolution class: 1440p",
        )

    def test_8k_shorthand(self):
        self.assertEqual(
            resolution_pool_key("8k"),
            "Resolution class: 8k",
        )

    def test_args_with_resolution_prefix(self):
        """Args like '1920x1080 [16:9]' should still match."""
        self.assertEqual(
            resolution_pool_key("1920x1080 [16:9]"),
            "Resolution class: 1080p-ish",
        )

    def test_args_with_flag_and_resolution(self):
        """Flag-style args like '--resolution 1920x1080' should match."""
        self.assertEqual(
            resolution_pool_key("--resolution 1920x1080"),
            "Resolution class: 1080p-ish",
        )

    def test_non_resolution_args_return_none(self):
        """Args without resolution info should return None."""
        self.assertIsNone(resolution_pool_key("--quality high"))
        self.assertIsNone(resolution_pool_key(""))
        self.assertIsNone(resolution_pool_key(None))
        self.assertIsNone(resolution_pool_key("foo bar"))

    def test_multiple_args_with_resolution_consistency(self):
        """Same resolution class for all 1080p-ish variants."""
        variants = ["1920x1080", "1920x1200", "1920x1080 [16:9]", "1920x1200 [16:10]"]
        keys = {resolution_pool_key(v) for v in variants}
        self.assertEqual(keys, {"Resolution class: 1080p-ish"})

    def test_resolution_uses_x_delimiter(self):
        """Handle various dimension delimiters."""
        for delim in ["x", "X", "×"]:
            self.assertEqual(
                resolution_pool_key(f"1920{delim}1080"),
                "Resolution class: 1080p-ish",
            )

    def test_canonicalize_preserves_non_resolution_parts(self):
        """_canonicalize_args_for_pool should keep non-resolution args intact."""
        canonical, changed = _canonicalize_args_for_pool("--preset medium 1920x1080")
        self.assertTrue(changed)
        self.assertIn("Resolution class: 1080p-ish", canonical)
        self.assertIn("--preset medium", canonical)

    def test_resolution_in_composite_args(self):
        """Args string with resolution plus other options."""
        self.assertEqual(
            resolution_pool_key("--quality ultra 1920x1080 --benchmark"),
            "Resolution class: 1080p-ish",
        )


class NativeResolutionExtractionTest(unittest.TestCase):

    def _make_system(self, software: str) -> SimpleNamespace:
        return SimpleNamespace(
            primary_system_name=None,
            identifier="test-system",
            hardware="Processor: Test CPU, Graphics: Test GPU",
            software=software,
            chassis_version=None,
            cooler_model=None,
            psu_wattage=None,
            psu_model=None,
            custom_hardware=None,
            external_off=False,
            gpu_fans=False,
            memory_fans=False,
            nvme_fans=False,
            nvme_configs=None,
        )

    def test_screen_resolution_field(self):
        """Phoronix-style 'Screen Resolution: 1920x1080' is detected."""
        sys = self._make_system(
            "OS: Pop 24.04, Kernel: 7.0.11-generic, "
            "Screen Resolution: 1920x1080"
        )
        comp = get_system_components(sys)
        self.assertEqual(comp["native_resolution"], "1920x1080")

    def test_display_field_with_refresh_rate(self):
        """'Display: 1920x1080@60Hz' strips the refresh rate."""
        sys = self._make_system(
            "OS: Ubuntu 24.04, Kernel: 6.8.0-generic, "
            "Display: 1920x1080@60Hz"
        )
        comp = get_system_components(sys)
        self.assertEqual(comp["native_resolution"], "1920x1080")

    def test_resolution_field(self):
        """Fallback 'Resolution: 2560x1440' is detected."""
        sys = self._make_system(
            "OS: Ubuntu 24.04, Resolution: 2560x1440"
        )
        comp = get_system_components(sys)
        self.assertEqual(comp["native_resolution"], "2560x1440")

    def test_no_resolution_info(self):
        """No display/resolution info returns empty string."""
        sys = self._make_system(
            "OS: Ubuntu 24.04, Kernel: 6.8.0-generic"
        )
        comp = get_system_components(sys)
        self.assertEqual(comp["native_resolution"], "")

    def test_screen_resolution_full_software_string(self):
        """Full realistic Phoronix software string."""
        sys = self._make_system(
            "OS: Pop 24.04, Kernel: 7.0.11-76070011-generic (x86_64), "
            "Desktop: COSMIC 1.0.12, Display Server: X Server + Wayland, "
            "Display Driver: NVIDIA 580.159.03, OpenGL: 4.6.0, "
            "Compiler: GCC 13.3.0, File-System: ext4, "
            "Screen Resolution: 1920x1080"
        )
        comp = get_system_components(sys)
        self.assertEqual(comp["native_resolution"], "1920x1080")

    def test_resolution_in_compare_by_options(self):
        """native_resolution should be present in get_system_components output."""
        sys = self._make_system(
            "Screen Resolution: 3840x2160"
        )
        comp = get_system_components(sys)
        self.assertIn("native_resolution", comp)
        self.assertEqual(comp["native_resolution"], "3840x2160")

    def test_screen_resolution_takes_precedence_over_display(self):
        """When 'Screen Resolution' is present, 'Display' is ignored (unusual but handled)."""
        sys = self._make_system(
            "Display: 2560x1440, Screen Resolution: 1920x1080"
        )
        comp = get_system_components(sys)
        # 'Screen Resolution' is checked first, so 1920x1080 wins
        self.assertEqual(comp["native_resolution"], "1920x1080")


if __name__ == "__main__":
    unittest.main()
