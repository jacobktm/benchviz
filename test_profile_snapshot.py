"""Tests for profile snapshot fingerprinting."""

import unittest

from app.profile_snapshot import capture_profile_snapshot, profile_fingerprint


class _FakeSystem:
    hardware = "Processor: AMD Ryzen 7 7800X3D\nGraphics: NVIDIA RTX 4080"
    primary_system_name = "test-rig"
    chassis_version = ""
    custom_hardware = ""
    cooler_model = "Noctua NH-D15"
    psu_model = "850W"
    psu_wattage = "850"
    external_off = False
    gpu_fans = False
    memory_fans = False
    nvme_fans = False
    manual_notes = ""


class TestProfileSnapshot(unittest.TestCase):
    def test_fingerprint_changes_with_cooler(self):
        a = _FakeSystem()
        b = _FakeSystem()
        b.cooler_model = "Stock cooler"
        fa = profile_fingerprint(capture_profile_snapshot(a))
        fb = profile_fingerprint(capture_profile_snapshot(b))
        self.assertNotEqual(fa, fb)


if __name__ == "__main__":
    unittest.main()
