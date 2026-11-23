from pathlib import Path
import tempfile
import unittest

from ops.optional_parsers import harden, verify


class OptionalParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = [self.root / "usr/bin/infocmp", self.root / "usr/share/perl/5.40.1/Archive/Tar.pm"]
        for path in (*self.paths, self.root / "usr/bin/git", self.root / "usr/share/perl5/Error.pm"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
            path.chmod(0o755)

    def tearDown(self):
        for directory in (self.root / "usr").rglob("*"):
            if directory.is_file() and not directory.is_symlink():
                directory.chmod(0o600)

    def test_only_unused_parsers_are_disabled_and_inventory_remains(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        report = harden(self.root)
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)
        self.assertEqual(verify(self.root)["disabled_files"], report["disabled_files"])
        self.assertFalse(report["upstream_patched"])
        self.assertEqual((self.root / "usr/bin/git").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / "usr/share/perl5/Error.pm").stat().st_mode & 0o777, 0o755)

    def test_every_installed_module_copy_is_disabled(self):
        extra = self.root / "usr/local/share/perl/5.40.1/Archive/Tar.pm"
        extra.parent.mkdir(parents=True)
        extra.write_text("second module")
        report = harden(self.root)
        self.assertEqual(len(report["disabled_files"]), 3)
        self.assertEqual(extra.stat().st_mode & 0o777, 0)

    def test_permission_regressions_are_rejected(self):
        harden(self.root)
        for path in self.paths:
            path.chmod(0o400)
            with self.assertRaises(ValueError):
                verify(self.root)
            path.chmod(0)

    def test_symlink_is_rejected_without_changing_target(self):
        target = self.root / "usr/bin/git"
        for path in self.paths:
            original = path.read_text()
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                harden(self.root)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            path.unlink()
            path.write_text(original)

    def test_missing_module_fails_closed(self):
        self.paths[-1].unlink()
        with self.assertRaises(ValueError):
            harden(self.root)


if __name__ == "__main__":
    unittest.main()
