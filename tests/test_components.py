"""Tests for hardware/software component extraction and normalization."""

import unittest
from unittest.mock import MagicMock

from app.components import (
    clean_text,
    get_primary_group_name,
    extract_hardware_component,
    normalize_processor_name,
    normalize_graphics_name,
    hardware_rank_match_key,
    extract_software_component,
    get_system_components,
)


class CleanTextTest(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(clean_text(None), '')

    def test_empty_returns_empty(self):
        self.assertEqual(clean_text(''), '')

    def test_strips_whitespace(self):
        self.assertEqual(clean_text('  hello  '), 'hello')

    def test_preserves_normal(self):
        self.assertEqual(clean_text('hello'), 'hello')


class GetPrimaryGroupNameTest(unittest.TestCase):
    def test_uses_primary_name(self):
        sys = MagicMock(primary_system_name='My System', identifier='sys1')
        self.assertEqual(get_primary_group_name(sys), 'My System')

    def test_falls_back_to_identifier(self):
        sys = MagicMock(primary_system_name=None, identifier='sys1')
        self.assertEqual(get_primary_group_name(sys), 'sys1')

    def test_falls_back_when_primary_empty(self):
        sys = MagicMock(primary_system_name='', identifier='sys2')
        self.assertEqual(get_primary_group_name(sys), 'sys2')


class ExtractHardwareComponentTest(unittest.TestCase):
    def test_none_hardware_returns_none(self):
        self.assertIsNone(extract_hardware_component(None, 'Processor'))

    def test_empty_hardware_returns_none(self):
        self.assertIsNone(extract_hardware_component('', 'Processor'))

    def test_extracts_processor(self):
        hw = 'Processor: AMD Ryzen 9 9950X, Memory: 32GB'
        self.assertEqual(
            extract_hardware_component(hw, 'Processor'),
            'AMD Ryzen 9 9950X',
        )

    def test_extracts_graphics(self):
        hw = 'Graphics: NVIDIA RTX 4090, Memory: 32GB'
        self.assertEqual(
            extract_hardware_component(hw, 'Graphics'),
            'NVIDIA RTX 4090',
        )

    def test_returns_none_for_missing(self):
        hw = 'Processor: CPU, Memory: 32GB'
        self.assertIsNone(extract_hardware_component(hw, 'Disk'))

    def test_extracts_memory(self):
        hw = 'Processor: CPU, Memory: 64GB'
        self.assertEqual(
            extract_hardware_component(hw, 'Memory'),
            '64GB',
        )

    def test_handles_extra_whitespace(self):
        hw = 'Processor:  Intel Core i7 , Memory: 16GB'
        self.assertEqual(
            extract_hardware_component(hw, 'Processor'),
            'Intel Core i7',
        )


class NormalizeProcessorNameTest(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(normalize_processor_name(''), '')

    def test_strips_clock_speed(self):
        self.assertEqual(
            normalize_processor_name('AMD Ryzen 9 9950X @ 5.76GHz'),
            'AMD Ryzen 9 9950X',
        )

    def test_strips_core_count(self):
        self.assertEqual(
            normalize_processor_name('AMD Ryzen 9 9950X 16-Core'),
            'AMD Ryzen 9 9950X',
        )

    def test_strips_core_plural(self):
        self.assertEqual(
            normalize_processor_name('Intel Core i9-14900K 24-Cores'),
            'Intel Core i9-14900K',
        )

    def test_strips_clock_and_cores(self):
        self.assertEqual(
            normalize_processor_name(
                'AMD Ryzen 9 9950X 16-Core @ 5.76GHz'
            ),
            'AMD Ryzen 9 9950X',
        )

    def test_handles_none(self):
        self.assertEqual(normalize_processor_name(None), '')

    def test_preserves_simple_name(self):
        self.assertEqual(
            normalize_processor_name('Intel Core i5-13600K'),
            'Intel Core i5-13600K',
        )


class NormalizeGraphicsNameTest(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(normalize_graphics_name(''), '')

    def test_strips_clock_speed(self):
        self.assertEqual(
            normalize_graphics_name('NVIDIA RTX 4090 @ 2520MHz'),
            'NVIDIA RTX 4090',
        )

    def test_strips_vram_gb(self):
        self.assertEqual(
            normalize_graphics_name('NVIDIA RTX 4090 24 GB'),
            'NVIDIA RTX 4090',
        )

    def test_strips_vram_mb(self):
        self.assertEqual(
            normalize_graphics_name('NVIDIA GTX 1060 6144 MB'),
            'NVIDIA GTX 1060',
        )

    def test_strips_trailing_gpu_word(self):
        self.assertEqual(
            normalize_graphics_name('NVIDIA RTX 5080 GPU'),
            'NVIDIA RTX 5080',
        )

    def test_preserves_laptop_in_name(self):
        """'Laptop' before 'GPU' should be preserved."""
        self.assertEqual(
            normalize_graphics_name('NVIDIA RTX 5080 Laptop GPU'),
            'NVIDIA RTX 5080 Laptop',
        )

    def test_handles_none(self):
        self.assertEqual(normalize_graphics_name(None), '')

    def test_strips_all_extras(self):
        self.assertEqual(
            normalize_graphics_name('AMD Radeon RX 7900 XTX 24 GB @ 2500MHz GPU'),
            'AMD Radeon RX 7900 XTX',
        )


class HardwareRankMatchKeyTest(unittest.TestCase):
    def test_processor_delegates_to_normalize_processor_name(self):
        result = hardware_rank_match_key('processor', 'AMD Ryzen 9 9950X 16-Core @ 5.76GHz')
        self.assertEqual(result, 'AMD Ryzen 9 9950X')

    def test_graphics_delegates_to_normalize_graphics_name(self):
        result = hardware_rank_match_key('graphics', 'NVIDIA RTX 4090 24 GB @ 2520MHz')
        self.assertEqual(result, 'NVIDIA RTX 4090')

    def test_unknown_key_returns_cleaned_text(self):
        result = hardware_rank_match_key('memory', '  32GB DDR5  ')
        self.assertEqual(result, '32GB DDR5')

    def test_none_key_falls_back_to_clean_text(self):
        """When the key is unmapped, the display value is returned as-is (cleaned)."""
        result = hardware_rank_match_key(None, 'text')
        self.assertEqual(result, 'text')

    def test_case_insensitive_key_matching(self):
        result = hardware_rank_match_key('Processor', 'AMD Ryzen 9')
        self.assertEqual(result, 'AMD Ryzen 9')


class ExtractSoftwareComponentTest(unittest.TestCase):
    def test_empty_software_returns_empty(self):
        self.assertEqual(extract_software_component('', 'Kernel'), '')

    def test_extracts_kernel(self):
        sw = 'OS: Ubuntu 24.04, Kernel: 6.8.0-45-generic'
        self.assertEqual(
            extract_software_component(sw, 'Kernel'),
            '6.8.0-45-generic',
        )

    def test_extracts_nvidia_driver(self):
        sw = 'OS: Windows 11, NVIDIA Driver: 560.70'
        self.assertEqual(
            extract_software_component(sw, 'NVIDIA Driver'),
            '560.70',
        )

    def test_extracts_mesa(self):
        sw = 'OS: Ubuntu, Mesa: 24.1.0'
        self.assertEqual(
            extract_software_component(sw, 'Mesa'),
            '24.1.0',
        )

    def test_returns_empty_for_missing_label(self):
        sw = 'OS: Linux, Kernel: 6.8'
        self.assertEqual(extract_software_component(sw, 'Vulkan'), '')

    def test_handles_newline_separated(self):
        sw = 'OS: Ubuntu 24.04\nKernel: 6.8.0\nMesa: 24.1.0'
        self.assertEqual(
            extract_software_component(sw, 'Kernel'),
            '6.8.0',
        )

    def test_case_insensitive_matching(self):
        sw = 'os: Ubuntu, kernel: 6.8'
        self.assertEqual(
            extract_software_component(sw, 'Kernel'),
            '6.8',
        )

    def test_handles_none_software(self):
        self.assertEqual(extract_software_component(None, 'Kernel'), '')

    def test_handles_none_label(self):
        self.assertEqual(extract_software_component('OS: Linux', None), '')


class GetSystemComponentsTest(unittest.TestCase):
    """Additional focused tests for get_system_components edge cases."""

    def _make_system(self, hardware='', software='', **profile):
        sys = MagicMock()
        sys.hardware = hardware
        sys.software = software
        sys.primary_system_name = None
        sys.identifier = 'test-sys'
        for k, v in profile.items():
            setattr(sys, k, v)
        sys.chassis_version = profile.get('chassis_version', None)
        sys.cooler_model = profile.get('cooler_model', None)
        sys.psu_wattage = profile.get('psu_wattage', None)
        sys.psu_model = profile.get('psu_model', None)
        sys.custom_hardware = profile.get('custom_hardware', None)
        sys.external_off = profile.get('external_off', False)
        sys.gpu_fans = profile.get('gpu_fans', False)
        sys.memory_fans = profile.get('memory_fans', False)
        sys.nvme_fans = profile.get('nvme_fans', False)
        sys.nvme_configs = profile.get('nvme_configs', [])
        return sys

    def test_missing_hardware_returns_empty_fields(self):
        sys = self._make_system(hardware='')
        comps = get_system_components(sys)
        self.assertEqual(comps['processor'], '')
        self.assertEqual(comps['graphics'], '')
        self.assertEqual(comps['memory'], '')

    def test_uses_identifier_when_no_primary_name(self):
        sys = self._make_system(identifier='my-machine')
        comps = get_system_components(sys)
        self.assertEqual(comps['system_name'], 'my-machine')

    def test_uses_primary_system_name(self):
        sys = self._make_system(identifier='ident', primary_system_name='Custom Name')
        comps = get_system_components(sys)
        self.assertEqual(comps['system_name'], 'Custom Name')

    def test_psu_combines_wattage_and_model(self):
        sys = self._make_system(psu_wattage='850W', psu_model='EVGA G5')
        comps = get_system_components(sys)
        self.assertEqual(comps['psu'], '850W EVGA G5')

    def test_external_off_flag(self):
        sys = self._make_system(external_off=True)
        comps = get_system_components(sys)
        self.assertEqual(comps['external_off'], 'Yes')

    def test_gpu_fans_flag(self):
        sys = self._make_system(gpu_fans=True)
        comps = get_system_components(sys)
        self.assertEqual(comps['gpu_fans'], 'Yes')

    def test_nvme_thermal_pads(self):
        pad = MagicMock()
        pad.top_thermal_pad = True
        pad.bottom_thermal_pad = False
        sys = self._make_system(nvme_configs=[pad])
        comps = get_system_components(sys)
        self.assertEqual(comps['thermal_pad_above_nvme'], 'Yes')
        self.assertEqual(comps['thermal_pad_below_nvme'], 'No')
        self.assertEqual(comps['thermal_pad_sandwich_nvme'], 'No')

    def test_nvme_sandwich_pads(self):
        pad = MagicMock()
        pad.top_thermal_pad = True
        pad.bottom_thermal_pad = True
        sys = self._make_system(nvme_configs=[pad])
        comps = get_system_components(sys)
        self.assertEqual(comps['thermal_pad_sandwich_nvme'], 'Yes')


if __name__ == "__main__":
    unittest.main()
