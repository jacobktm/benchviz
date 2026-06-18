"""Smoke test: verify all endpoints and redirects resolve correctly.

Catches issues like missing blueprint prefixes in url_for() calls
(regression detected in the routes→blueprint refactor).
"""

import unittest
from flask import url_for

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




class UrlForResolveTest(unittest.TestCase):
    """Verify all url_for() calls in route handlers resolve (no missing blueprint prefix)."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.ctx = cls.app.test_request_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def _assert_resolves(self, endpoint: str, **values):
        try:
            url_for(endpoint, **values)
        except Exception as e:
            self.fail(f"url_for({endpoint!r}, {values!r}) raised: {e}")

    # ── pages blueprint ──────────────────────────────────────────────

    def test_dashboard(self):
        self._assert_resolves('pages.dashboard')

    def test_upload(self):
        self._assert_resolves('pages.upload_benchmarks')

    def test_system_detail(self):
        self._assert_resolves('pages.system_detail', id=1)

    def test_update_system(self):
        self._assert_resolves('pages.update_system', id=1)

    def test_delete_system(self):
        self._assert_resolves('pages.delete_system', id=1)

    def test_delete_system_benchmark(self):
        self._assert_resolves('pages.delete_system_benchmark', system_id=1)

    def test_compare_saved(self):
        self._assert_resolves('pages.compare_saved', comp_id='test')

    def test_list_saved_comparisons(self):
        self._assert_resolves('pages.list_saved_comparisons')

    def test_delete_saved_comparison(self):
        self._assert_resolves('pages.delete_saved_comparison', comp_id='test')

    # ── export blueprint ─────────────────────────────────────────────

    def test_export_slide(self):
        self._assert_resolves('export.export_slide')

    def test_export_slides(self):
        self._assert_resolves('export.list_export_slides')

    def test_delete_export_slide(self):
        self._assert_resolves('export.delete_export_slide', export_id='test')

    # ── api blueprint (common endpoints) ─────────────────────────────

    def test_api_compare(self):
        self._assert_resolves('api.api_compare')




class PostRouteSmokeTest(unittest.TestCase):
    """Smoke-test POST route handlers.

    Verifies no unhandled exceptions (500) when calling POST endpoints
    with invalid / nonexistent data.  A 404 or 4xx is acceptable.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    # ── pages blueprint ──────────────────────────────────────────────

    def test_upload_post(self):
        """POST /upload without files → redirect (302) or error, not 500."""
        resp = self.client.post('/upload')
        self.assertNotEqual(500, resp.status_code)

    def test_update_system_missing(self):
        """POST /update_system/<missing> → 404, not 500."""
        resp = self.client.post('/update_system/99999', data={})
        self.assertNotEqual(500, resp.status_code)

    def test_delete_system_missing(self):
        """POST /delete_system/<missing> → 404, not 500."""
        resp = self.client.post('/delete_system/99999')
        self.assertNotEqual(500, resp.status_code)

    def test_delete_benchmark_missing(self):
        """POST /system/<missing>/delete_benchmark → 404, not 500."""
        resp = self.client.post('/system/99999/delete_benchmark',
                                data={'title': 'x'})
        self.assertNotEqual(500, resp.status_code)

    def test_delete_saved_comparison_missing(self):
        """POST /compare/saved/<missing>/delete → flash+redirect, not 500."""
        resp = self.client.post('/compare/saved/nonexistent/delete')
        self.assertNotEqual(500, resp.status_code)

    # ── export blueprint ─────────────────────────────────────────────

    def test_export_slide_no_image(self):
        """POST /export/slide without image → 400, not 500."""
        resp = self.client.post('/export/slide', data={})
        self.assertEqual(400, resp.status_code)

    def test_export_slide_delete_missing(self):
        """POST /export/slide/<missing>/delete → flash+redirect, not 500."""
        resp = self.client.post('/export/slide/nonexistent/delete')
        self.assertNotEqual(500, resp.status_code)

    # ── api blueprint ────────────────────────────────────────────────

    def test_api_save_comparison_empty(self):
        """POST /api/save_comparison with empty payload → 400, not 500."""
        resp = self.client.post('/api/save_comparison',
                                json={"systems": [], "benchmarks": []})
        self.assertEqual(400, resp.status_code)


if __name__ == '__main__':
    unittest.main()
