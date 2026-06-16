import unittest

from app.hardware_slug import (
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


if __name__ == '__main__':
    unittest.main()
