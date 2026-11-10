import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from git import Actor, Repo
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agent.tools import RepoDeps, make_tools
from indexer import code_search, snapshots
from indexer.code_search import find_in_snapshot
from indexer.source_reader import SourceReadError, read_source_bytes

URL = "https://github.com/example/search-fixture"
SOURCE = '''import os
FLAG = "needle"
TOKEN = os.getenv("TOKEN", "")
class Worker:
    async def handle(self):
        def nested():
            return FLAG
        return nested()
def worker():
    return FLAG
'''


class CodeSearchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / "work"
        self.repo = Repo.init(self.work)
        self.addCleanup(self.repo.close)
        files = {
            "app.py": SOURCE, "bad.py": "def invalid(\nneedle\n", "binary.py": "binary\x00needle",
            "docs/readme.md": "中文关键词 Straße\nneedle needle\n",
            "long.md": "x" * 9000 + "\nEND_MARKER\n",
            "ui.js": "function handle() { return 'needle'; }\n",
            "unsupported.txt": "HIDDEN_UNSUPPORTED_EXTENSION", "vendor/hidden.py": "HIDDEN_VENDOR",
            "large.py": "x" * (code_search.MAX_FILE_BYTES + 1),
            "unsafe.py": f"from pathlib import Path\nPath({str(self.root / 'executed')!r}).write_text('BAD')\n",
        }
        for name, content in files.items():
            target = self.work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.repo.index.add(list(files))
        self.actor = Actor("Fixture", "fixture@example.invalid")
        self.repo.index.commit("search fixture", author=self.actor, committer=self.actor)
        context = patch.object(snapshots, "CLONE_DIR", str(self.root / "snapshots"))
        context.start()
        self.addCleanup(context.stop)
        self.snapshot = snapshots.create_snapshot(URL, str(self.work))

    def find(self, query="needle", **kwargs):
        return find_in_snapshot(self.snapshot.root, URL, self.snapshot.commit_sha, query, **kwargs)

    def test_text_finds_imports_and_module_config_missing_from_function_chunks(self):
        hit = self.find("os.getenv(", path_filter="app.py").hits[0]
        self.assertEqual((hit.file_path, hit.start_line, hit.end_line), ("app.py", 3, 3))
        self.assertIn("TOKEN = os.getenv", hit.preview)
        self.assertEqual(self.find("import os", path_filter="app.py").hits[0].start_line, 1)

    def test_path_filter_case_options_and_no_glob_interpretation(self):
        result = self.find("APP.PY", mode="path")
        self.assertEqual([hit.file_path for hit in result.hits], ["app.py"])
        self.assertIsNone(result.hits[0].start_line)
        self.assertFalse(self.find("APP.PY", mode="path", case_sensitive=True).hits)
        self.assertFalse(self.find("*.py", mode="path").hits)
        self.assertEqual([hit.file_path for hit in self.find("needle", path_filter="docs/").hits], ["docs/readme.md"])

    def test_python_definitions_include_qualified_nested_and_async_names(self):
        nested = self.find("Worker.handle.nested", mode="symbol").hits[0]
        self.assertEqual((nested.start_line, nested.end_line), (6, 7))
        method = self.find("Worker.handle", mode="symbol", limit=1).hits[0]
        self.assertEqual((method.kind, method.symbol, method.start_line), ("function", "Worker.handle", 5))
        cls = self.find("Worker", mode="symbol", case_sensitive=True).hits[0]
        self.assertEqual((cls.kind, cls.symbol), ("class", "Worker"))
        self.assertTrue(all(hit.file_path.endswith(".py") for hit in self.find("handle", mode="symbol").hits))

    def test_syntax_errors_and_binary_and_size_skips_are_reported(self):
        result = self.find("needle")
        skipped = {item.file_path: item.reason for item in result.skipped_examples}
        self.assertEqual(skipped["binary.py"], "binary_file")
        self.assertEqual(skipped["large.py"], "file_too_large")
        self.assertIn("bad.py", [hit.file_path for hit in result.hits])
        result = self.find("handle", mode="symbol")
        self.assertIn("python_parse_error", [item.reason for item in result.skipped_examples])

    def test_supported_extensions_and_directory_exclusions(self):
        self.assertFalse(self.find("HIDDEN").hits)
        self.assertFalse(self.find("unsupported", mode="path").hits)
        self.assertFalse(self.find("vendor", mode="path").hits)
        self.assertIn("ui.js", [hit.file_path for hit in self.find("needle").hits])

    def test_pagination_has_no_duplicates_and_is_stable(self):
        all_hits = self.find("FLAG", path_filter="app.py").hits
        first = self.find("FLAG", path_filter="app.py", limit=1)
        second = self.find("FLAG", path_filter="app.py", limit=1, offset=first.next_offset)
        third = self.find("FLAG", path_filter="app.py", limit=1, offset=second.next_offset)
        self.assertEqual(first.hits + second.hits + third.hits, all_hits)
        self.assertIsNone(third.next_offset)
        self.assertFalse(self.find("FLAG", path_filter="app.py", offset=999).hits)

    def test_one_hit_per_matching_line_and_unicode_casefold(self):
        self.assertEqual(len(self.find("needle", path_filter="docs/").hits), 1)
        self.assertEqual(self.find("STRASSE", path_filter="docs/").hits[0].start_line, 1)
        self.assertTrue(self.find("中文关键词").hits)

    def test_search_reads_past_excerpt_limit_but_preview_is_bounded(self):
        self.assertEqual(self.find("END_MARKER").hits[0].start_line, 2)
        hit = self.find("xxx", path_filter="long.md").hits[0]
        self.assertTrue(hit.preview_truncated)
        self.assertEqual(len(hit.preview), code_search.MAX_PREVIEW_CHARS)

    def test_scan_limits_are_explicit_and_filter_can_narrow_scope(self):
        with patch.object(code_search, "MAX_SCAN_FILES", 1):
            result = self.find("END_MARKER")
            self.assertTrue(result.scan_limited)
            self.assertFalse(result.hits)
            self.assertTrue(self.find("END_MARKER", path_filter="long.md").hits)
        with patch.object(code_search, "MAX_SCAN_BYTES", 10):
            result = self.find("needle", path_filter="app.py")
            self.assertTrue(result.scan_limited)
            self.assertFalse(result.hits)

    def test_invalid_inputs_are_rejected(self):
        for changes in (dict(query=""), dict(query="\n"), dict(query="x" * 257), dict(mode="regex"),
                        dict(limit=0), dict(limit=True), dict(limit=51), dict(offset=-1), dict(offset=False),
                        dict(case_sensitive=1), dict(path_filter="\x00")):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.find(**changes)

    def test_old_version_is_unchanged_and_local_edits_are_not_searched(self):
        (self.work / "app.py").write_text(SOURCE.replace("needle", "NEW_VERSION"))
        (self.work / "untracked.py").write_text("UNTRACKED_VALUE")
        self.repo.index.add(["app.py"])
        self.repo.index.commit("new source", author=self.actor, committer=self.actor)
        newer = snapshots.create_snapshot(URL, str(self.work))
        self.assertTrue(self.find("needle", path_filter="app.py").hits)
        result = find_in_snapshot(newer.root, URL, newer.commit_sha, "NEW_VERSION", path_filter="app.py")
        self.assertEqual(result.commit_sha, newer.commit_sha)
        self.assertTrue(result.hits)
        self.assertFalse(self.find("UNTRACKED_VALUE").hits)

    def test_wrong_identity_tampering_and_symlinks_are_rejected(self):
        with self.assertRaises(ValueError):
            find_in_snapshot(self.snapshot.root, URL, "0" * 40, "needle")
        file = Path(self.snapshot.root) / "app.py"
        file.chmod(0o644)
        file.write_text("TAMPERED")
        with self.assertRaisesRegex(SourceReadError, "changed"):
            self.find("TAMPERED", path_filter="app.py")
        file.unlink()
        file.symlink_to(self.work / "app.py")
        with self.assertRaisesRegex(SourceReadError, "Symbolic"):
            self.find("needle", path_filter="app.py")

    def test_search_does_not_execute_target_source(self):
        self.find("Path(", path_filter="unsafe.py")
        self.find("Path", mode="symbol", path_filter="unsafe.py")
        self.assertFalse((self.root / "executed").exists())

    def test_shared_byte_reader_keeps_path_guard_and_enforces_limit(self):
        with self.assertRaisesRegex(SourceReadError, "byte limit"):
            read_source_bytes(self.snapshot.root, "long.md", 100)
        with self.assertRaises(SourceReadError):
            read_source_bytes(self.snapshot.root, "../outside", 100)

    def test_agent_and_mcp_share_search_and_lookup_does_not_create_citations(self):
        import mcp_server
        functions = {}
        make_tools(SimpleNamespace(tool=lambda function: functions.setdefault(function.__name__, function)))
        deps = RepoDeps(URL, self.snapshot.root, "fixture", self.snapshot.commit_sha)
        raw = asyncio.run(functions["find_code"](SimpleNamespace(deps=deps), "os.getenv(", path_filter="app.py"))
        with patch.object(mcp_server, "_current_repository", SimpleNamespace(commit_sha=self.snapshot.commit_sha)), patch.object(mcp_server, "_current_repo_root", self.snapshot.root), patch.object(mcp_server, "_current_repo_url", URL):
            self.assertEqual(json.loads(raw), mcp_server.find_code("os.getenv(", path_filter="app.py"))
        self.assertFalse(deps.evidence.check_answer("Unchecked preview [S1]").can_save)

    def test_framework_finds_then_reads_then_cites(self):
        from agent import qa_agent
        turns = []
        def model(messages, info):
            turns.append(len(messages))
            self.assertIn("find_code", [tool.name for tool in info.function_tools])
            if len(turns) == 1:
                return ModelResponse(parts=[ToolCallPart("find_code", {"query": "os.getenv(", "path_filter": "app.py"}, "find")])
            if len(turns) == 2:
                returned = next(part for message in reversed(messages) for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "find_code")
                hit = json.loads(returned.content)["hits"][0]
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": hit["file_path"], "start_line": hit["start_line"], "end_line": hit["end_line"]}, "read")])
            return ModelResponse(parts=[TextPart("app.py reads TOKEN from the environment [S1]")])
        agent = Agent(FunctionModel(model), deps_type=RepoDeps)
        make_tools(agent)
        results = []
        async def collect():
            return "".join([part async for part in qa_agent.ask_stream("Where is TOKEN?", URL, self.snapshot.root,
                collection_name="fixture", commit_sha=self.snapshot.commit_sha, on_result=results.append)])
        with patch.object(qa_agent, "_get_agent", return_value=agent), patch.object(qa_agent, "save_qa"):
            answer = asyncio.run(collect())
        self.assertEqual(len(turns), 3)
        self.assertTrue(results[0].can_save)
        self.assertIn(f"/blob/{self.snapshot.commit_sha}/app.py#L3-L3", answer)


if __name__ == "__main__":
    unittest.main()
