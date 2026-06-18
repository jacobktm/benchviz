import unittest

from app.hardware_slug import (
    abbreviate_disk,
    abbreviate_graphics,
    abbreviate_memory,
    abbreviate_processor,
    build_hardware_slug,
    profile_identifier,
)
from app.system_util import base_system_identifier


class HardwareSlugTest(unittest.TestCase):
    def test_abbreviate_intel_core_i5(self):
        self.assertEqual(
            abbreviate_processor('Intel Core i5-13600K 14-Core @ 5.1GHz'),
            'ci5-136k',
        )

    def test_abbreviate_core_ultra_hx_plus(self):
        self.assertEqual(
            abbreviate_processor('Intel Core Ultra 9 290HX-Plus'),
            'cu9-29hxp',
        )

    def test_abbreviate_amd_ryzen(self):
        self.assertEqual(
            abbreviate_processor('AMD Ryzen 9 9950X 16-Core'),
            'ar9-9950x',
        )

    def test_abbreviate_memory_single_dimm(self):
        self.assertEqual(
            abbreviate_memory('1 x 32GB DDR5 @ 5600 MT/s'),
            '1x32g56',
        )

    def test_abbreviate_memory_dual_dimm(self):
        self.assertEqual(
            abbreviate_memory('2 x 16GB DDR5-4800'),
            '2x16g48',
        )

    def test_build_hardware_slug_combines_parts(self):
        hardware = (
            'Processor: Intel Core i5-13600K, '
            'Memory: 1 x 32GB DDR5 @ 5600 MT/s, '
            'Graphics: NVIDIA GeForce RTX 4080'
        )
        slug = build_hardware_slug(hardware, serial_number='ABC123')
        self.assertIn('ci5-136k', slug)
        self.assertIn('1x32g56', slug)
        self.assertIn('rtx4080', slug)
        self.assertIn('snabc123', slug)

    def test_profile_identifier_joins_base_and_slug(self):
        self.assertEqual(
            profile_identifier('qa-lemp13', 'ci5-136k-1x32g56'),
            'qa-lemp13__ci5-136k-1x32g56',
        )

    def test_base_system_identifier_strips_hardware_suffix(self):
        self.assertEqual(
            base_system_identifier('qa-lemp13__ci5-136k-1x32g56'),
            'qa-lemp13',
        )

    def test_abbreviate_graphics_laptop(self):
        self.assertEqual(
            abbreviate_graphics('NVIDIA GeForce RTX 5080 Laptop GPU 16GB'),
            'rtx5080l',
        )

    # ── disk abbreviation ───────────────────────────────────────────

    def test_abbreviate_samsung_disk(self):
        self.assertEqual(
            abbreviate_disk('Samsung SSD 990 Pro 2TB'),
            's-990pro-2t',
        )

    def test_abbreviate_wd_disk(self):
        self.assertEqual(
            abbreviate_disk('WD Blue SN580 2TB'),
            'wd-bluesn580-2t',
        )

    def test_abbreviate_crucial_disk(self):
        self.assertEqual(
            abbreviate_disk('Crucial T700 1TB'),
            'c-t700-1t',
        )

    def test_abbreviate_unknown_disk(self):
        self.assertEqual(abbreviate_disk(''), '')

    def test_build_hardware_slug_includes_disk(self):
        slug = build_hardware_slug(
            'Processor: Intel Core i5-13600K, '
            'Memory: 1 x 32GB DDR5 @ 5600 MT/s, '
            'Graphics: NVIDIA GeForce RTX 4080, '
            'Disk: Samsung SSD 990 Pro 2TB',
            serial_number='ABC123',
        )
        self.assertIn('s-990pro-2t', slug)
        self.assertIn('ci5-13k', slug)
        self.assertIn('1x32g56', slug)
        self.assertIn('rtx4080', slug)
        self.assertIn('snabc123', slug)

    def test_build_hardware_slug_multiple_disks(self):
        slug = build_hardware_slug(
            'Processor: Intel Core i5-13600K, '
            'Memory: 1 x 32GB DDR5 @ 5600 MT/s, '
            'Graphics: NVIDIA GeForce RTX 4080, '
            'Disk: Samsung SSD 990 Pro 2TB + Samsung SSD 980 Pro 1TB',
        )
        self.assertIn('s-990pro-2t', slug)
        self.assertIn('s-980pro-1t', slug)


if __name__ == '__main__':
    unittest.main()
