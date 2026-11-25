import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ops import python_expat_build as builder

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / 'deploy/python-expat.json').read_text())


class PythonExpatBuildTests(unittest.TestCase):
    def test_baseline_source_and_headers_are_bound_before_building(self):
        with patch.object(builder.sys, 'version_info', (3, 12, 13)), \
             patch.dict(builder.os.environ, {'PYTHON_SHA256': MANIFEST['source_sha256']}):
            builder.check_baseline(MANIFEST)
            for change in ({'source_sha256': 'wrong'}, {'source_url': 'https://example.invalid/python.tar.xz'},
                           {'headers_url': 'https://example.invalid/headers.deb'}, {'expat_version': 'expat_2.9.0'}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    builder.check_baseline(dict(MANIFEST, **change))

    def response(self, data, url='https://example.invalid/source'):
        response = io.BytesIO(data)
        response.geturl = lambda: url
        opener = Mock()
        opener.open.return_value = response
        return opener

    def test_streamed_download_checks_hash_and_size_and_removes_failed_file(self):
        data = b'fixture' * 20000
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'source'
            self.assertEqual(builder.fetch('https://example.invalid/source', digest, path, len(data),
                                          opener=self.response(data)), len(data))
            self.assertEqual(path.read_bytes(), data)
            path.unlink()
            for expected_hash, limit in (('wrong', len(data)), (digest, len(data)-1)):
                with self.assertRaises(ValueError):
                    builder.fetch('https://example.invalid/source', expected_hash, path, limit, opener=self.response(data))
                self.assertFalse(path.exists())

    def test_redirect_is_rejected_before_creating_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'source'
            with self.assertRaises(ValueError):
                builder.fetch('https://example.invalid/source', '', path, 20, opener=self.response(b'data', 'https://other.invalid/'))
            self.assertFalse(path.exists())

    def test_download_never_overwrites_or_deletes_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'source'
            path.write_bytes(b'KEEP')
            with self.assertRaises(FileExistsError):
                builder.fetch('https://example.invalid/source', '', path, 20, opener=self.response(b'data'))
            self.assertEqual(path.read_bytes(), b'KEEP')

    def test_official_test_success_cannot_hide_missing_c_accelerator(self):
        summary = '\nRan 792 tests in 1.281s\n\nOK (skipped=1)\n'
        with self.assertRaisesRegex(ValueError, 'C accelerator'):
            builder.check_test_log("test_c ... skipped 'requires _elementtree'\n" + summary)
        self.assertEqual(builder.check_test_log("test_windows ... skipped 'platform'\n" + summary), (792, 1.281, 1))


if __name__ == '__main__':
    unittest.main()
