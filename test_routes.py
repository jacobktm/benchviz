"""Smoke test: verify all page routes render without 500 errors.

Catches template-rendering issues like missing blueprint prefixes in
url_for() calls (regression detected in the routes→blueprint refactor).
"""

import unittest

from app import create_app
from app.ob_cache_sync import lookup_ob_entry_with_fallback


class RouteSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    def _assert_not_500(self, path: str, method: str = 'GET'):
        resp = self.client.open(path, method=method)
        self.assertNotEqual(
            resp.status_code, 500,
            f'{method} {path} returned 500 (likely template / url_for error)',
        )

    # ── pages blueprint ──────────────────────────────────────────────

    def test_dashboard(self):
        self._assert_not_500('/')

    def test_upload(self):
        self._assert_not_500('/upload')

    def test_compare(self):
        self._assert_not_500('/compare')

    def test_compare_saved_list(self):
        self._assert_not_500('/compare/saved')

    def test_insights(self):
        self._assert_not_500('/insights')

    # ── export blueprint ─────────────────────────────────────────────

    def test_export_slides(self):
        self._assert_not_500('/export/slides')




class ObCacheRegressionTest(unittest.TestCase):
    """Regression tests for the ob_cache_sync refactor."""

    def test_lookup_with_dict_entry_does_not_crash(self):
        """
        lookup_ob_entry_with_fallback must handle dict cache entries
        without assuming they are Path objects.

        Regression: _index_entry_cache_fresh(ent) was accidentally replaced
        with _is_cache_fresh(ent), which calls .is_file() on a dict.
        """
        index = {
            "entries": {
                "some-hash": {
                    "test_profile": "pts/example-1.0",
                    "app_version": "1.0",
                    "description": "Test",
                    "unit": "Seconds",
                    "samples": 10,
                    "percentiles": [0] * 51,
                    "ob_median": 100.0,
                },
            },
            "fallback_buckets": {},
        }
        entry, source = lookup_ob_entry_with_fallback(
            "some-hash", index, allow_live=False,
        )
        self.assertIsNotNone(entry, "Entry should be returned from index")
        self.assertEqual(source, "local")


if __name__ == '__main__':
    unittest.main()
