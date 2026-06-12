"""Unit tests for MONITOR argument matching (no database)."""

import unittest

from app.workload_profile import _args_matches_config, _monitor_result_matches_config


class TestMonitorArgsMatch(unittest.TestCase):
    def test_primary_args_match_exact(self):
        opt = "FasterRCNN-12-int8/FasterRCNN-12-int8.onnx -e cpu"
        self.assertTrue(_monitor_result_matches_config(opt, opt))

    def test_monitor_prefix_matches_option(self):
        opt = "FasterRCNN-12-int8/FasterRCNN-12-int8.onnx -e cpu"
        mon = f"CPU Usage (Summary) {opt}  -P"
        self.assertTrue(_monitor_result_matches_config(mon, opt))

    def test_empty_config_accepts_prefixed_monitor(self):
        mon = "CPU Usage (Summary) Aircrack-ng  -P"
        self.assertFalse(_args_matches_config(mon, ""))
        self.assertTrue(_monitor_result_matches_config(mon, ""))

    def test_empty_config_accepts_empty_monitor(self):
        self.assertTrue(_monitor_result_matches_config("", ""))
        self.assertTrue(_monitor_result_matches_config(None, ""))


if __name__ == "__main__":
    unittest.main()
