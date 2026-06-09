import unittest

from app import create_app, db
from app.models import System
from app.system_util import hardware_fingerprint, resolve_system_for_import


class SystemUtilTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_same_identifier_and_hardware_reuses_system(self):
        first, created, _ = resolve_system_for_import(
            'qa-meer10',
            'Processor: AMD Ryzen 9 9950X',
            'OS: Pop!_OS',
            'user',
            '2026-01-01',
        )
        self.assertTrue(created)
        db.session.commit()

        second, created, note = resolve_system_for_import(
            'qa-meer10',
            'Processor:  AMD   Ryzen 9 9950X',
            'OS: Pop!_OS 24.04',
            'user2',
            '2026-02-01',
        )
        self.assertFalse(created)
        self.assertIsNone(note)
        self.assertEqual(first.id, second.id)
        self.assertEqual(System.query.count(), 1)

    def test_same_identifier_different_hardware_creates_suffix(self):
        resolve_system_for_import(
            'qa-meer10',
            'Processor: AMD Ryzen 9 9950X',
            '',
            '',
            '',
        )
        db.session.commit()

        second, created, note = resolve_system_for_import(
            'qa-meer10',
            'Processor: Intel Core i9-14900K',
            '',
            '',
            '',
        )
        db.session.commit()

        self.assertTrue(created)
        self.assertEqual(second.identifier, 'qa-meer10 (2)')
        self.assertIn('Hardware differs', note or '')
        self.assertEqual(System.query.count(), 2)

    def test_hardware_fingerprint_normalizes_whitespace(self):
        self.assertEqual(
            hardware_fingerprint('A  B'),
            hardware_fingerprint('a b'),
        )


if __name__ == '__main__':
    unittest.main()
