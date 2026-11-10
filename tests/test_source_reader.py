import asyncio
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indexer.source_reader import MAX_READ_CHARS, SourceReadError, read_source_file


class SourceReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.source = self.root / "example.py"
        self.source.write_text("first\n\nthird\n", encoding="utf-8")

    def read(self, name="example.py", start=1, end=None):
        return read_source_file(str(self.root), name, start, end)

    def test_numbered_range_and_hash(self):
        excerpt = self.read(start=2, end=3)
        self.assertEqual(excerpt.content, "\nthird")
        self.assertEqual((excerpt.start_line, excerpt.end_line, excerpt.total_lines), (2, 3, 3))
        self.assertIn("2 | \n3 | third", excerpt.format())
        self.assertEqual(excerpt.file_sha256, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_can_read_after_the_old_character_limit(self):
        self.source.write_text("padding_" * 1200 + "\nlast_function()\n")
        excerpt = self.read(start=2, end=2)
        self.assertEqual(excerpt.content, "last_function()")
        self.assertFalse(excerpt.truncated)

    def test_full_file_remains_bounded_and_can_continue(self):
        self.source.write_text("\n".join(["x" * 99] * 200))
        excerpt = self.read()
        self.assertLessEqual(len(excerpt.content), MAX_READ_CHARS)
        self.assertTrue(excerpt.truncated)
        self.assertFalse(excerpt.partial_last_line)
        self.assertIn(f"start_line={excerpt.end_line + 1}", excerpt.format())
        remaining = self.read(start=excerpt.end_line + 1)
        self.assertEqual(remaining.start_line, excerpt.end_line + 1)

    def test_partial_giant_line_is_explicit(self):
        self.source.write_text("x" * (MAX_READ_CHARS + 1))
        excerpt = self.read()
        self.assertTrue(excerpt.partial_last_line)
        self.assertIn("last line is partial", excerpt.format())

    def test_end_is_clamped_to_file_length(self):
        self.assertEqual(self.read(end=100).end_line, 3)

    def test_invalid_ranges_are_rejected(self):
        for start, end in [(0, None), (-1, 2), (True, None), (1, False), (3, 2), (4, None)]:
            with self.subTest(start=start, end=end), self.assertRaises(SourceReadError):
                self.read(start=start, end=end)

    def test_empty_file(self):
        self.source.write_text("")
        self.assertEqual(self.read().total_lines, 0)
        self.assertIn("File is empty", self.read().format())

    def test_missing_directory_and_binary_are_rejected(self):
        for name in ("missing.py", "."):
            with self.subTest(name=name), self.assertRaises(SourceReadError):
                self.read(name)
        self.source.write_bytes(b"binary\x00data")
        with self.assertRaises(SourceReadError):
            self.read()

    def test_escape_absolute_paths_and_git_metadata_are_rejected(self):
        outside = Path(self.temp.name) / "secret.txt"
        outside.write_text("OUTSIDE_SECRET")
        for name in ("../secret.txt", str(outside), "C:\\secret.txt", ".git/config"):
            with self.subTest(name=name), self.assertRaises(SourceReadError):
                self.read(name)

    def test_symlinks_are_rejected(self):
        (self.root / "linked.py").symlink_to(self.source)
        with self.assertRaises(SourceReadError):
            self.read("linked.py")

    def test_content_change_changes_identity(self):
        before = self.read()
        self.source.write_text("changed\n")
        self.assertNotEqual(before.file_sha256, self.read().file_sha256)

    def test_agent_and_mcp_use_the_same_reader(self):
        from agent.tools import RepoDeps, make_tools
        import mcp_server

        functions = {}
        def register(function):
            functions[function.__name__] = function
            return function
        make_tools(SimpleNamespace(tool=register))
        context = SimpleNamespace(deps=RepoDeps("https://github.com/example/repo", str(self.root)))
        agent_result = asyncio.run(functions["read_file"](context, "example.py", 2, 3))
        with patch.object(mcp_server, "_current_repo_root", str(self.root)):
            self.assertEqual(agent_result, mcp_server.read_file("example.py", 2, 3))
            self.assertIn("Only repository-relative", mcp_server.read_file("../secret.txt"))


if __name__ == "__main__":
    unittest.main()
