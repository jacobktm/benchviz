import json
import unittest

from app import create_app, db
from app.models import Benchmark, BenchmarkResult, System
from app.route_helpers.compare import _reconcile_primary_name_conflict
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
        self.assertEqual(second.identifier, 'qa-meer10__ci9-149k')
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


class SystemMergeTest(unittest.TestCase):
    """Tests for system merge on primary_system_name conflict."""

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

    def _make_system(self, **kw) -> System:
        s = System(
            identifier=kw.get('identifier', 'test-sys'),
            primary_system_name=kw.get('primary_system_name', 'Test System'),
            hardware=kw.get('hardware', 'Processor: Test CPU, Graphics: Test GPU'),
            software=kw.get('software', 'OS: TestOS 1.0'),
        )
        db.session.add(s)
        db.session.flush()
        return s

    def test_merge_identical_systems_keeps_one(self):
        """Two systems with same primary_system_name and identical HW/SW merge into one."""
        s1 = self._make_system(identifier='sys-a', primary_system_name='Foo')
        s2 = self._make_system(identifier='sys-b', primary_system_name='Foo')
        db.session.commit()

        _reconcile_primary_name_conflict('Foo')
        db.session.commit()

        remaining = System.query.all()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].identifier, 'Foo')

    def test_merge_moves_benchmark_results(self):
        """BenchmarkResults from the deleted system are reassigned to the survivor."""
        s1 = self._make_system(identifier='survivor', primary_system_name='Bar')
        s2 = self._make_system(identifier='deleted', primary_system_name='Bar')
        bm = Benchmark(identifier='pts/test-1.0.0', title='Test', description='', scale='', proportion='LIB', display_format='BAR_GRAPH')
        db.session.add(bm)
        db.session.flush()
        br = BenchmarkResult(system_id=s2.id, benchmark_id=bm.id, arguments='', value=42.0)
        db.session.add(br)
        db.session.commit()

        self.assertEqual(BenchmarkResult.query.count(), 1)
        self.assertEqual(BenchmarkResult.query.first().system_id, s2.id)

        _reconcile_primary_name_conflict('Bar')
        db.session.commit()

        self.assertEqual(BenchmarkResult.query.count(), 1)
        self.assertEqual(BenchmarkResult.query.first().system_id, s1.id)

    def test_no_merge_when_hardware_differs(self):
        """Systems with same primary_system_name but different HW stay separate."""
        s1 = self._make_system(primary_system_name='Baz', hardware='Processor: Intel')
        s2 = self._make_system(primary_system_name='Baz', hardware='Processor: AMD')
        db.session.commit()

        _reconcile_primary_name_conflict('Baz')
        db.session.commit()

        self.assertEqual(System.query.count(), 2)

    def test_update_system_redirect_after_merge(self):
        """POST /update_system on a merged system redirects to the survivor, not 404."""
        s1 = self._make_system(identifier='target', primary_system_name='Merged')
        s2 = self._make_system(identifier='source', primary_system_name='Other')
        bm = Benchmark(identifier='pts/bench-1.0', title='Bench', description='', scale='', proportion='LIB', display_format='BAR_GRAPH')
        db.session.add(bm)
        db.session.flush()
        br = BenchmarkResult(system_id=s2.id, benchmark_id=bm.id, arguments='', value=99.0)
        db.session.add(br)
        db.session.commit()

        client = self.app.test_client()
        resp = client.post(f'/update_system/{s2.id}', data={
            'identifier': 'source',
            'primary_system_name': 'Merged',
        }, follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get('Location', '')
        # The redirect should point to system s1 (the survivor), not s2 (deleted)
        self.assertIn(f'/system/{s1.id}', location,
                      f'Expected redirect to survivor system {s1.id}, got {location}')

        # The deleted system should no longer exist
        self.assertIsNone(System.query.get(s2.id))

    def test_update_system_redirect_no_merge(self):
        """Simple identifier change (no merge) redirects to the same system."""
        s = self._make_system(identifier='original', primary_system_name='Unchanged')
        db.session.commit()

        client = self.app.test_client()
        resp = client.post(f'/update_system/{s.id}', data={
            'identifier': 'renamed',
            'primary_system_name': 'Unchanged',
        }, follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get('Location', '')
        self.assertIn(f'/system/{s.id}', location)

        # Verify the identifier was actually updated
        db.session.refresh(s)
        self.assertEqual(s.identifier, 'renamed')


if __name__ == '__main__':
    unittest.main()
