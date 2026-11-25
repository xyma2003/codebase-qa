import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ops import expat_update as update
from ops.expat_check import parse_version

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "deploy/expat-update.json").read_text())


class ExpatUpdateTests(unittest.TestCase):
    def inventories(self):
        before = {'libexpat1': {'status': 'installed', 'version': MANIFEST['before']},
                  'libc6': {'status': 'installed', 'version': '2.41-12+deb13u5'}}
        after = copy.deepcopy(before)
        after['libexpat1']['version'] = MANIFEST['after']
        return before, after

    def test_inventory_change_is_limited_to_exact_expat_upgrade(self):
        before, after = self.inventories()
        self.assertEqual(set(update.validate_changes(before, after, MANIFEST)), {'libexpat1'})
        for field in ('version', 'status'):
            changed = copy.deepcopy(after)
            changed['libc6'][field] = 'unexpected'
            with self.assertRaises(ValueError):
                update.validate_changes(before, changed, MANIFEST)
        for changed in (before, dict(after, unexpected={'status': 'installed', 'version': '1'})):
            with self.assertRaises(ValueError):
                update.validate_changes(before, changed, MANIFEST)

    def test_download_checks_redirect_size_and_sha_before_writing(self):
        manifest = dict(MANIFEST, bytes=7, sha256=hashlib.sha256(b'fixture').hexdigest())
        for data, url, passed in ((b'fixture', manifest['url'], True), (b'changed', manifest['url'], False),
                                  (b'fixture!', manifest['url'], False), (b'fixture', 'https://example.invalid/', False)):
            with tempfile.TemporaryDirectory() as directory, self.subTest(data=data, url=url):
                response = io.BytesIO(data)
                response.geturl = lambda: url
                opener = Mock()
                opener.open.return_value = response
                destination = Path(directory)/'package.deb'
                if passed:
                    update.download(manifest, destination, opener=opener)
                    self.assertEqual(destination.read_bytes(), b'fixture')
                else:
                    with self.assertRaises(ValueError):
                        update.download(manifest, destination, opener=opener)
                    self.assertFalse(destination.exists())

    def test_download_rejects_non_debian_url_without_network(self):
        opener = Mock()
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            update.download(dict(MANIFEST, url='https://example.invalid/package.deb'), Path(directory)/'deb', opener=opener)
        opener.open.assert_not_called()

    def fake_run(self, *, invalid_field=None, wrong_baseline=False):
        before, after = self.inventories()
        if wrong_baseline:
            before['libexpat1']['version'] = 'unexpected'
        inventories = iter((before, after))
        fields = {'Package': 'libexpat1', 'Source': 'expat', 'Version': MANIFEST['after'],
                  'Architecture': 'amd64', 'Pre-Depends': MANIFEST['pre_depends']}
        calls = []
        def run(arguments):
            calls.append(arguments)
            if arguments[:2] == ['dpkg', '--print-architecture']:
                return 'amd64'
            if arguments[0] == 'dpkg-query':
                return '\n'.join(f"{k}:amd64\t{v['status']}\t{v['version']}" for k, v in next(inventories).items())
            if arguments[0] == 'dpkg-deb':
                return 'unexpected' if arguments[-1] == invalid_field else fields[arguments[-1]]
            return ''
        return run, calls

    def test_install_uses_dpkg_only_and_retains_package_provenance(self):
        run, calls = self.fake_run()
        report = update.install(MANIFEST, run=run, fetch=Mock())
        self.assertEqual(report['package_sha256'], MANIFEST['sha256'])
        self.assertFalse(report['other_packages_changed'])
        self.assertFalse(report['apt_sources_changed'])
        self.assertEqual(sum(c[:2] == ['dpkg', '--install'] for c in calls), 1)
        self.assertFalse(any(c[0].startswith('apt') for c in calls))

    def test_wrong_package_metadata_or_baseline_refuses_before_install(self):
        for field in ('Package', 'Source', 'Version', 'Architecture', 'Pre-Depends'):
            run, calls = self.fake_run(invalid_field=field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                update.install(MANIFEST, run=run, fetch=Mock())
            self.assertFalse(any(c[:2] == ['dpkg', '--install'] for c in calls))
        run, calls = self.fake_run(wrong_baseline=True)
        fetch = Mock()
        with self.assertRaises(ValueError):
            update.install(MANIFEST, run=run, fetch=fetch)
        fetch.assert_not_called()

    def test_original_dpkg_error_is_preserved_without_retry(self):
        result = subprocess.CompletedProcess(['dpkg'], 1, 'INSTALL_FIXTURE\n', 'dependency libc6 not satisfied')
        with patch.object(update.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(ValueError, 'dependency libc6 not satisfied'):
                update.command(['dpkg', '--install', 'fixture.deb'])
            self.assertEqual(run.call_count, 1)

    def test_successful_dpkg_exit_with_integrity_findings_retains_original_output(self):
        original, calls = self.fake_run()
        def run(arguments):
            if arguments == ['dpkg', '--verify', 'libexpat1']:
                calls.append(arguments)
                return 'VERIFY_FIXTURE unexpected file digest'
            return original(arguments)
        with self.assertRaisesRegex(ValueError, 'VERIFY_FIXTURE unexpected file digest'):
            update.install(MANIFEST, run=run, fetch=Mock())
        self.assertEqual(sum(c == ['dpkg', '--verify', 'libexpat1'] for c in calls), 1)

    def test_native_versions_are_compared_numerically_and_unknown_versions_fail(self):
        self.assertGreater(parse_version('expat_2.11.0'), parse_version('expat_2.9.0'))
        self.assertEqual(parse_version('2.11.0'), (2, 11, 0))
        with self.assertRaises(ValueError):
            parse_version('unknown')


if __name__ == '__main__':
    unittest.main()
