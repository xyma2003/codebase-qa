import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ops import openssl_build as builder, openssl_update as updater
from ops.openssl_check import tls_check

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'deploy/openssl-update.json').read_text())


class OpenSSLBackportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_official_patch_identity_and_hash_must_both_match(self):
        patches = self.root / 'patches'
        shutil.copytree(ROOT / 'deploy/openssl-patches', patches)
        self.assertEqual(len(builder.check_patches(MANIFEST, patches)), 3)
        first = patches / MANIFEST['patches'][0]['file']
        first.write_bytes(first.read_bytes() + b'changed')
        with self.assertRaises(ValueError):
            builder.check_patches(MANIFEST, patches)
        bad = copy.deepcopy(MANIFEST)
        bad['patches'][0]['file'] = '../outside.patch'
        with self.assertRaises(ValueError):
            builder.check_patches(bad, patches)

    def test_native_quic_pass_cannot_hide_missing_or_skipped_regression(self):
        good = 'ok 17 - new_connid_retrans\nAll tests successful.\nResult: PASS\n'
        self.assertEqual(builder.check_quic_log(good), ['ok 17 - new_connid_retrans'])
        for bad in ('All tests successful.\nResult: PASS', good.replace('ok 17', 'ok 17 # SKIP'),
                    good.replace('Result: PASS', 'Result: FAIL')):
            with self.subTest(log=bad), self.assertRaises(ValueError):
                builder.check_quic_log(bad)

    def inventories(self):
        before = {name: {'status': 'installed', 'version': MANIFEST['source_version']}
                  for name in MANIFEST['runtime_packages']}
        before['libc6'] = {'status': 'installed', 'version': '2.41-12+deb13u5'}
        after = copy.deepcopy(before)
        for name in MANIFEST['runtime_packages']:
            after[name]['version'] = MANIFEST['package_version']
        return before, after

    def test_only_the_three_exact_package_versions_can_change(self):
        before, after = self.inventories()
        self.assertEqual(set(updater.validate_changes(before, after, MANIFEST)), updater.PACKAGES)
        for name in (*MANIFEST['runtime_packages'], 'libc6'):
            bad = copy.deepcopy(after)
            bad[name]['version'] = 'unexpected'
            with self.subTest(package=name), self.assertRaises(ValueError):
                updater.validate_changes(before, bad, MANIFEST)
        with self.assertRaises(ValueError):
            updater.validate_changes(before, before, MANIFEST)

    def fixture_build(self):
        directory = self.root / 'packages'
        directory.mkdir()
        packages = {}
        for name in sorted(updater.PACKAGES):
            path = directory / (name + '_' + MANIFEST['package_version'] + '_amd64.deb')
            path.write_text('PACKAGE FIXTURE ' + name)
            packages[name] = {'file': path.name, 'sha256': builder.digest(path),
                              'version': MANIFEST['package_version'], 'architecture': 'amd64'}
        report = {'packages': packages, 'patches': MANIFEST['patches'],
                  'source_dsc_sha256': MANIFEST['source_files'][0]['sha256'],
                  'package_version': MANIFEST['package_version'], 'debian_signature_required': True,
                  'quic_regression_lines': ['ok - new_connid_retrans'],
                  'gc_control': {'baseline': {'exit_code': 1}, 'patched': {'exit_code': 0}},
                  'native_suite_totals': {'files': 1, 'tests': 10},
                  'runtime_files_sha256': {name: 'a'*64 for name in MANIFEST['runtime_files']}}
        return directory, report

    def test_missing_signature_native_control_or_package_hash_refuses_install(self):
        directory, report = self.fixture_build()
        self.assertEqual(len(updater.check_build(MANIFEST, report, directory)), 3)
        for key, value in (('debian_signature_required', False), ('quic_regression_lines', []),
                           ('native_suite_totals', None), ('runtime_files_sha256', {}),
                           ('gc_control', {'baseline': {'exit_code': 0}, 'patched': {'exit_code': 0}})):
            bad = copy.deepcopy(report)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                updater.check_build(MANIFEST, bad, directory)
        path = directory / report['packages']['openssl']['file']
        path.write_text('TAMPERED')
        with self.assertRaises(ValueError):
            updater.check_build(MANIFEST, report, directory)

    def test_extra_staged_file_is_never_installed(self):
        directory, report = self.fixture_build()
        (directory / 'other.deb').write_text('unexpected')
        with self.assertRaises(ValueError):
            updater.check_build(MANIFEST, report, directory)

    def fake_run(self, report, *, verify_output=''):
        before, after = self.inventories()
        values = iter((before, after))
        calls = []
        def run(arguments):
            calls.append(arguments)
            if arguments == ['dpkg', '--print-architecture']:
                return 'amd64'
            if arguments[0] == 'dpkg-query':
                return '\n'.join(f"{k}\t{v['status']}\t{v['version']}" for k, v in next(values).items())
            if arguments[:2] == ['dpkg', '--verify']:
                return verify_output
            if arguments[0] == 'dpkg-deb':
                name = Path(arguments[2]).name.split('_')[0]
                return {'Package': name, 'Source': 'openssl', 'Version': MANIFEST['package_version'],
                        'Architecture': 'amd64'}[arguments[-1]]
            return ''
        return run, calls

    def test_install_cannot_fetch_other_dependencies_or_ignore_integrity_errors(self):
        directory, report = self.fixture_build()
        run, calls = self.fake_run(report)
        original_digest = updater.digest
        def digest(path):
            return original_digest(path) if Path(path).parent == directory else 'a'*64
        with patch.object(updater, 'digest', side_effect=digest):
            result = updater.install(MANIFEST, report, directory, run=run)
        self.assertFalse(result['other_packages_changed'])
        installs = [c for c in calls if c[0] == 'dpkg' and '--install' in c]
        self.assertEqual(len(installs), 1)
        self.assertEqual(sum(str(p).endswith('.deb') for p in installs[0]), 3)
        self.assertFalse(any(c[0].startswith('apt') for c in calls))
        run, calls = self.fake_run(report, verify_output='VERIFY_FIXTURE missing library')
        with self.assertRaisesRegex(ValueError, 'VERIFY_FIXTURE missing library'):
            updater.install(MANIFEST, report, directory, run=run)

    def test_real_local_tls_verifies_trust_and_hostname(self):
        result = tls_check()
        self.assertTrue(result['verified_round_trip'])
        self.assertTrue(result['wrong_hostname_rejected'])
        self.assertTrue(result['untrusted_certificate_rejected'])


if __name__ == '__main__':
    unittest.main()
