import copy
import unittest
from ops.image_comparison import compare
from tests import test_os_triage as triage_fixtures


class ImageComparisonTests(unittest.TestCase):
    def setUp(self):
        fixture = triage_fixtures.OsTriageTests(); fixture.setUp()
        self.before = fixture.raw
        self.after = copy.deepcopy(self.before)
        self.after['Metadata']['ImageID'] = 'sha256:'+'b'*64
        self.after['Results'][0]['Packages'] = [p for p in self.after['Results'][0]['Packages'] if p['Name']!='mount']
        self.after['Results'][0]['Vulnerabilities'] = [v for v in self.after['Results'][0]['Vulnerabilities'] if v['PkgName']!='mount']

    def test_removal_counts_records_and_preserves_other_affected_packages(self):
        report = compare(self.before,self.after,['mount'])
        self.assertEqual((report['baseline_records'],report['candidate_records']), (2,1))
        self.assertEqual((report['baseline_ids'],report['candidate_ids']), (1,1))
        self.assertFalse(report['public_release_allowed'])

    def test_unrelated_package_version_change_is_rejected(self):
        self.after['Results'][0]['Packages'][-1]['Version']='9'
        with self.assertRaises(ValueError): compare(self.before,self.after,['mount'])

    def test_package_still_present_and_distribution_change_are_rejected(self):
        with self.assertRaises(ValueError): compare(self.before,self.before,['mount'])
        self.after['Metadata']['OS']['Name']='14'
        with self.assertRaises(ValueError): compare(self.before,self.after,['mount'])

    def test_explicit_version_update_is_allowed_but_not_reported_as_unchanged(self):
        after = copy.deepcopy(self.before)
        after['Results'][0]['Packages'][-1]['Version'] = '2.42'
        report = compare(self.before, after, [], {'libc6': {'before': '2.41', 'after': '2.42'}})
        self.assertEqual(report['package_changes'], {'libc6': {'before': '2.41', 'after': '2.42'}})
        self.assertEqual(report['unchanged_os_packages'], 2)
        self.assertFalse(report['public_release_allowed'])

    def test_declared_update_must_match_both_exact_versions_and_presence(self):
        for update in ({'before': 'wrong', 'after': '2.42'}, {'before': '2.41', 'after': 'wrong'},
                       {'before': '2.41', 'after': '2.42', 'extra': 'bad'}):
            after = copy.deepcopy(self.before)
            after['Results'][0]['Packages'][-1]['Version'] = '2.42'
            with self.subTest(update=update), self.assertRaises(ValueError):
                compare(self.before, after, [], {'libc6': update})
        with self.assertRaises(ValueError):
            compare(self.before, self.before, [], {'missing': {'before': '1', 'after': '2'}})

    def test_declared_update_cannot_allow_other_package_changes(self):
        after = copy.deepcopy(self.before)
        after['Results'][0]['Packages'][-1]['Version'] = '2.42'
        after['Results'][0]['Packages'][0]['Version'] = '9'
        after['Results'][0]['Vulnerabilities'][0]['InstalledVersion'] = '9'
        with self.assertRaises(ValueError):
            compare(self.before, after, [], {'libc6': {'before': '2.41', 'after': '2.42'}})
