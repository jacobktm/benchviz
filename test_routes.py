"""Smoke test: verify all page routes render without 500 errors.

Catches template-rendering issues like missing blueprint prefixes in
url_for() calls (regression detected in the routes→blueprint refactor).
"""

import unittest

from app import create_app


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


if __name__ == '__main__':
    unittest.main()
