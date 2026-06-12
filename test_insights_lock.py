"""Tests for cross-process insights rebuild lock."""

import os
import tempfile
import unittest
from unittest.mock import patch

from app.insights_lock import insights_lock_path, insights_rebuild_lock


class TestInsightsRebuildLock(unittest.TestCase):
    def test_nonblocking_second_acquire_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = os.path.join(tmp, "rebuild-insights.lock")
            with patch("app.insights_lock.insights_lock_path", return_value=lock_file):
                with insights_rebuild_lock(block=False) as first:
                    self.assertTrue(first)
                    with insights_rebuild_lock(block=False) as second:
                        self.assertFalse(second)

    def test_lock_path_under_instance(self):
        self.assertTrue(insights_lock_path().endswith(os.path.join("instance", "rebuild-insights.lock")))


if __name__ == "__main__":
    unittest.main()
