import unittest

from app import create_app, db
from app.models import System
from app.system_util import base_system_identifier, hardware_fingerprint, resolve_system_for_import


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

    def test_same_identifier_different_hardware_adds_profile(self):
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
        self.assertEqual(second.identifier, 'qa-meer10__ci9-14900k')
        self.assertEqual(second.primary_system_name, 'qa-meer10')
        self.assertIn('hardware-distinguished', note or '')
        self.assertEqual(System.query.count(), 2)

    def test_same_identifier_same_hardware_different_serial_adds_profile(self):
        resolve_system_for_import(
            'qa-lemp13',
            'Processor: Intel Core Ultra 7 265K',
            '',
            '',
            '',
            serial_number='SN-1001',
        )
        db.session.commit()

        second, created, note = resolve_system_for_import(
            'qa-lemp13',
            'Processor: Intel Core Ultra 7 265K',
            '',
            '',
            '',
            serial_number='SN-1002',
        )
        db.session.commit()

        self.assertTrue(created)
        self.assertEqual(second.identifier, 'qa-lemp13__cu7-265k-sn1002')
        self.assertEqual(second.primary_system_name, 'qa-lemp13')
        self.assertEqual(second.serial_number, 'SN-1002')
        self.assertIn('hardware-distinguished', (note or '').lower())
        self.assertEqual(System.query.count(), 2)

    def test_same_identifier_hardware_and_serial_reuses_profile(self):
        first, _, _ = resolve_system_for_import(
            'qa-lemp13',
            'Processor: Intel Core Ultra 7 265K',
            '',
            '',
            '',
            serial_number='SN-1001',
        )
        db.session.commit()

        second, created, _ = resolve_system_for_import(
            'qa-lemp13',
            'Processor: Intel Core Ultra 7 265K',
            'OS: Pop',
            '',
            '',
            serial_number='sn-1001',
        )
        db.session.commit()

        self.assertFalse(created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(System.query.count(), 1)

    def test_base_system_identifier_strips_legacy_suffix(self):
        self.assertEqual(base_system_identifier('qa-meer10 (2)'), 'qa-meer10')
        self.assertEqual(base_system_identifier('qa-meer10'), 'qa-meer10')
        self.assertEqual(base_system_identifier('qa-meer10__ar9-9950x'), 'qa-meer10')

    def test_hardware_fingerprint_normalizes_whitespace(self):
        self.assertEqual(
            hardware_fingerprint('A  B'),
            hardware_fingerprint('a b'),
        )

    def test_group_system_profiles_uses_primary_system_name(self):
        from app.route_helpers import group_system_profiles

        db.session.add_all([
            System(
                identifier='Mira-R4-N3',
                primary_system_name='Mira R4',
                hardware='Processor: AMD Ryzen 9 9950X',
                software='OS: Pop!_OS',
            ),
            System(
                identifier='Mira-R4-N4',
                primary_system_name='Mira R4',
                hardware='Processor: AMD Ryzen 9 9950X',
                software='OS: Pop!_OS',
            ),
            System(
                identifier='pang14',
                primary_system_name='pang14',
                hardware='Processor: Intel Core i7',
                software='OS: Ubuntu',
            ),
        ])
        db.session.commit()

        groups = group_system_profiles(System.query.all())
        by_name = {g['group_name']: g for g in groups}
        self.assertEqual(len(by_name['Mira R4']['profiles']), 2)
        self.assertEqual(len(by_name['pang14']['profiles']), 1)


if __name__ == '__main__':
    unittest.main()
