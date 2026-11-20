import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ops.privileged_tools import BLOCKED, harden, verify


class PrivilegedToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "usr/bin").mkdir(parents=True)
        for name in (*BLOCKED, "git", "helper"):
            path = self.root / "usr/bin" / name
            path.write_bytes(b"fixture")
            path.chmod(0o6755 if name == "helper" else 0o755)

    def test_hardening_blocks_all_four_tools_and_removes_setid_only(self):
        report = harden(self.root)
        self.assertFalse(report["upstream_patched"])
        self.assertEqual(verify(self.root)["setid_files"], 0)
        self.assertEqual((self.root / "usr/bin/git").stat().st_mode & 0o7777, 0o755)
        self.assertEqual((self.root / "usr/bin/helper").stat().st_mode & 0o7777, 0o755)

    def test_permission_regression_and_new_setid_files_fail_closed(self):
        harden(self.root)
        for name in BLOCKED:
            path = self.root / "usr/bin" / name
            path.chmod(0o100)
            with self.assertRaises(ValueError):
                verify(self.root)
            path.chmod(0)
        # The Mac sandbox strips set-ID bits on chmod. Linux image checks also
        # enumerate the real files, while this fixture exercises rejection.
        original = Path.lstat
        def with_setid(path):
            value = original(path)
            if path == self.root / "usr/bin/git":
                return os.stat_result((value.st_mode | 0o4000, *tuple(value)[1:]))
            return value
        with patch.object(Path, "lstat", with_setid), self.assertRaises(ValueError):
            verify(self.root)

    def test_hardening_never_follows_tool_or_directory_symlinks(self):
        path = self.root / "usr/bin/mount"
        path.unlink()
        path.symlink_to(self.root / "usr/bin/git")
        with self.assertRaises(ValueError):
            harden(self.root)
        self.assertEqual((self.root / "usr/bin/git").stat().st_mode & 0o7777, 0o755)


if __name__ == "__main__":
    unittest.main()
