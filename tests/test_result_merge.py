"""Tests for distinct upload-batch result rows."""

import statistics
import unittest
from types import SimpleNamespace

from app.models import BenchmarkResult
from app.result_merge import (
    assign_bar_graph_result,
    assign_line_graph_result,
    bar_run_values,
    extract_run_values_from_entry,
)


class TestResultMerge(unittest.TestCase):
    def test_assign_bar_graph_from_entry(self):
        row = BenchmarkResult(system_id=1, benchmark_id=1)
        entry = SimpleNamespace(
            findtext=lambda key, default="": {
                "RawString": "10.0:11.0",
                "JSON": "",
            }.get(key, default),
        )
        assign_bar_graph_result(row, entry, "10.5")
        self.assertEqual(row.data_json, [10.0, 11.0])
        self.assertAlmostEqual(row.value, statistics.mean([10.0, 11.0]))

    def test_assign_line_graph(self):
        row = BenchmarkResult(system_id=1, benchmark_id=2)
        assign_line_graph_result(row, [1.0, 2.0, 3.0])
        self.assertEqual(row.data_json, [1.0, 2.0, 3.0])

    def test_bar_run_values_flat_list(self):
        self.assertEqual(bar_run_values([1.0, 2.0, 3.0], 99.0), [1.0, 2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
