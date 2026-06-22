from __future__ import annotations

import json
import os
import tempfile
import unittest

from app.hardware_spec import (
    auto_populate_hardware_spec,
    apply_sidecar_to_spec,
    is_sidecar_filename,
    parse_cpu_spec,
    parse_gpu_spec,
    parse_memory_spec,
    spec_get,
    system_id_from_sidecar_filename,
)
from app.models import HardwareSpec


class TestParseCpuSpec(unittest.TestCase):
    def test_amd_ryzen_standard(self):
        hw = "Processor: AMD Ryzen 9 9950X 16-Core @ 5.76GHz (16 Cores / 32 Threads)"
        result = parse_cpu_spec(hw)
        self.assertEqual(result['_flat']['cpu_model'], "AMD Ryzen 9 9950X 16-Core @ 5.76GHz (16 Cores / 32 Threads)")
        self.assertEqual(result['_flat']['cpu_cores'], 16)
        self.assertEqual(result['_flat']['cpu_threads'], 32)
        self.assertEqual(result['_spec']['boost_clock_mhz'], 5760)
        self.assertEqual(result['_spec']['model'], "AMD Ryzen 9 9950X")
        self.assertEqual(len(result['_spec']['clusters']), 1)
        self.assertEqual(result['_spec']['clusters'][0]['cores'], 16)
        self.assertEqual(result['_spec']['clusters'][0]['threads'], 32)
        self.assertEqual(result['_spec']['clusters'][0]['boost_clock_mhz'], 5760)

    def test_intel_hybrid(self):
        hw = "Processor: Intel Core Ultra 9 285K @ 5.28GHz (24 Cores / 24 Threads)"
        result = parse_cpu_spec(hw)
        self.assertEqual(result['_flat']['cpu_cores'], 24)
        self.assertEqual(result['_flat']['cpu_threads'], 24)
        self.assertEqual(result['_spec']['boost_clock_mhz'], 5280)
        # Single cluster by default; user fills cluster details via schema
        self.assertEqual(len(result['_spec']['clusters']), 1)

    def test_threadripper(self):
        hw = "Processor: AMD Ryzen Threadripper PRO 7955WX 16-Cores @ 5.38GHz (16 Cores / 32 Threads)"
        result = parse_cpu_spec(hw)
        self.assertEqual(result['_spec']['model'], "AMD Ryzen Threadripper PRO 7955WX")
        self.assertEqual(result['_flat']['cpu_cores'], 16)
        self.assertEqual(result['_flat']['cpu_threads'], 32)
        self.assertEqual(result['_spec']['boost_clock_mhz'], 5380)

    def test_no_cpu_data(self):
        result = parse_cpu_spec("")
        self.assertEqual(result, {})
        result = parse_cpu_spec(None)
        self.assertEqual(result, {})

    def test_mhz_clock(self):
        hw = "Processor: Some CPU @ 4200MHz (8 Cores / 16 Threads)"
        result = parse_cpu_spec(hw)
        self.assertEqual(result['_spec']['boost_clock_mhz'], 4200.0)

    def test_memory(self):
        hw = "Memory: 131072MB DDR5 @ 5200MHz"
        result = parse_memory_spec(hw)
        self.assertEqual(result['_spec']['size_mb'], 131072)
        self.assertEqual(result['_spec']['type'], "DDR5")
        self.assertEqual(result['_spec']['speed_mhz'], 5200)
        self.assertIsNone(result['_spec'].get('channels'))

    def test_memory_gb(self):
        hw = "Memory: 32768MB DDR4 @ 3600MHz (2x16384MB)"
        result = parse_memory_spec(hw)
        self.assertEqual(result['_spec']['size_mb'], 32768)
        self.assertEqual(result['_spec']['speed_mhz'], 3600)
        self.assertEqual(result['_spec'].get('channels'), 2)

    def test_no_memory(self):
        self.assertEqual(parse_memory_spec(""), {})
        self.assertEqual(parse_memory_spec(None), {})


class TestParseGpuSpec(unittest.TestCase):
    def test_nvidia(self):
        hw = "Graphics: NVIDIA GeForce RTX 5080 16GB @ 2.52GHz"
        result = parse_gpu_spec(hw)
        self.assertEqual(result['_flat']['gpu_model'], "NVIDIA GeForce RTX 5080 16GB @ 2.52GHz")
        self.assertEqual(result['_spec']['vram_mb'], 16384)
        self.assertEqual(result['_spec']['boost_clock_mhz'], 2520)
        self.assertEqual(result['_spec']['model'], "NVIDIA GeForce RTX 5080")

    def test_amd_gpu(self):
        hw = "Graphics: AMD Radeon RX 7900 XTX 24GB"
        result = parse_gpu_spec(hw)
        self.assertEqual(result['_spec']['vram_mb'], 24576)
        self.assertEqual(result['_spec']['model'], "AMD Radeon RX 7900 XTX")

    def test_no_gpu(self):
        self.assertEqual(parse_gpu_spec(""), {})

    def test_mb_vram(self):
        hw = "Graphics: Intel Arc A770 16384MB"
        result = parse_gpu_spec(hw)
        self.assertEqual(result['_spec']['vram_mb'], 16384)


class TestSidecarFilenames(unittest.TestCase):
    def test_is_sidecar(self):
        self.assertTrue(is_sidecar_filename('hardware-spec-thelio-mega-r4.json'))
        self.assertTrue(is_sidecar_filename('hardware-spec-my-system.json'))
        self.assertFalse(is_sidecar_filename('composite.xml'))
        self.assertFalse(is_sidecar_filename('test.xml'))
        self.assertTrue(is_sidecar_filename('hardware-spec-.json'))  # syntactically valid, just empty identifier

    def test_extract_system_id(self):
        self.assertEqual(system_id_from_sidecar_filename('hardware-spec-thelio-mega-r4.json'), 'thelio-mega-r4')
        self.assertEqual(system_id_from_sidecar_filename('hardware-spec-my-machine.json'), 'my-machine')
        self.assertEqual(system_id_from_sidecar_filename('hardware-spec-.json'), '')


class TestApplySidecarToSpec(unittest.TestCase):
    def setUp(self):
        self.spec = HardwareSpec(
            system_id=1,
            cpu_model="Old CPU",
            cpu_spec={"boost_clock_mhz": 5000},
            gpu_spec={"vram_mb": 8192},
        )

    def test_update_flat_field(self):
        apply_sidecar_to_spec(self.spec, {"cpu": {"model": "New CPU"}})
        self.assertEqual(self.spec.cpu_model, "New CPU")

    def test_update_json_blob_field(self):
        apply_sidecar_to_spec(self.spec, {"cpu": {"arch_family": "zen_5"}})
        self.assertEqual(self.spec.cpu_spec["arch_family"], "zen_5")

    def test_update_multiple(self):
        apply_sidecar_to_spec(self.spec, {
            "cpu": {"arch_family": "zen_5", "boost_clock_mhz": 5750},
            "gpu": {"vram_mb": 16384, "shader_count": 10752},
        })
        self.assertEqual(self.spec.cpu_spec["arch_family"], "zen_5")
        self.assertEqual(self.spec.cpu_spec["boost_clock_mhz"], 5750)
        self.assertEqual(self.spec.gpu_spec["vram_mb"], 16384)
        self.assertEqual(self.spec.gpu_spec["shader_count"], 10752)

    def test_preserves_existing_blob_keys(self):
        apply_sidecar_to_spec(self.spec, {"cpu": {"arch_family": "zen_5"}})
        self.assertEqual(self.spec.cpu_spec["boost_clock_mhz"], 5000)
        self.assertEqual(self.spec.cpu_spec["arch_family"], "zen_5")

    def test_storage_array(self):
        storage = [{"model": "Samsung 990 Pro", "type": "nvme", "capacity_gb": 2048}]
        apply_sidecar_to_spec(self.spec, {"storage": storage})
        self.assertEqual(self.spec.storage_spec, storage)

    def test_ignore_unknown_keys(self):
        apply_sidecar_to_spec(self.spec, {"cpu": {"nonexistent_field": 42}})
        self.assertEqual(self.spec.cpu_spec.get("nonexistent_field"), 42)


class TestSpecGet(unittest.TestCase):
    def test_spec_get(self):
        spec = HardwareSpec(system_id=1, cpu_spec={"arch_family": "zen_5"})
        self.assertEqual(spec_get(spec, "cpu_spec", "arch_family"), "zen_5")
        self.assertIsNone(spec_get(spec, "cpu_spec", "nonexistent"))
        self.assertIsNone(spec_get(None, "cpu_spec", "arch_family"))

    def test_missing_blob(self):
        spec = HardwareSpec(system_id=1)
        self.assertIsNone(spec_get(spec, "cpu_spec", "arch_family"))


class TestRealWorldPtsHardware(unittest.TestCase):
    """Parse realistic multi-line PTS hardware strings."""

    FULL_HW = (
        "Processor: AMD Ryzen Threadripper PRO 7955WX 16-Cores @ 5.38GHz (16 Cores / 32 Threads), "
        "Graphics: NVIDIA GeForce RTX 5080 16GB @ 2.61GHz, "
        "Memory: 131072MB DDR5, "
        "Motherboard: ASUS Pro WS TRX50-SAGE WIFI, "
        "Chipset: AMD TRX50, "
        "Disk: 2048GB + 2048GB"
    )

    def test_full_parse(self):
        cpu = parse_cpu_spec(self.FULL_HW)
        gpu = parse_gpu_spec(self.FULL_HW)
        mem = parse_memory_spec(self.FULL_HW)

        self.assertEqual(cpu['_flat']['cpu_model'], "AMD Ryzen Threadripper PRO 7955WX 16-Cores @ 5.38GHz (16 Cores / 32 Threads)")
        self.assertEqual(cpu['_flat']['cpu_cores'], 16)
        self.assertEqual(cpu['_flat']['cpu_threads'], 32)
        self.assertEqual(cpu['_spec']['boost_clock_mhz'], 5380)

        self.assertEqual(gpu['_flat']['gpu_model'], "NVIDIA GeForce RTX 5080 16GB @ 2.61GHz")
        self.assertEqual(gpu['_spec']['vram_mb'], 16384)
        self.assertEqual(gpu['_spec']['boost_clock_mhz'], 2610)

        self.assertEqual(mem['_spec']['size_mb'], 131072)
        self.assertEqual(mem['_spec']['type'], "DDR5")
        self.assertIsNone(mem['_spec'].get('speed_mhz'))  # no @ speed in this string
        self.assertIsNone(mem['_spec'].get('channels'))


if __name__ == '__main__':
    unittest.main()
