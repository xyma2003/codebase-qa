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
from indexer import dependencies, snapshots
from indexer.citations import check_source_citation
from indexer.dependencies import dependency_payload, get_file_dependencies
from indexer.source_reader import SourceReadError

URL = "https://github.com/example/import-fixture"
MAIN = '''import cfg as settings
from pkg.mod import value
from pkg import sub
from pkg import exported
from pkg import *
if False:
    import pkg.mod
def lazy():
    import pkg.sub
import importlib as il
il.import_module("pkg.mod")
__import__(selected_name)
'''


class DependencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / "work"
        self.repo = Repo.init(self.work)
        self.addCleanup(self.repo.close)
        self.actor = Actor("Fixture", "fixture@example.invalid")
        files = {
            "main.py": MAIN, "cfg.py": "VALUE = 1\n", "pkg/__init__.py": "from .mod import value as exported\n",
            "pkg/mod.py": "value = 1\n", "pkg/sub.py": "import cfg\n", "namespace/mod.py": "value = 1\n",
            "ns_user.py": "from namespace import mod\n", "collision.py": "value = 1\n", "collision/__init__.py": "value = 2\n",
            "conflict_user.py": "import collision\n", "src/spkg/__init__.py": "", "src/spkg/b.py": "value = 1\n",
            "src/spkg/a.py": "import spkg.b\nfrom .b import value\nfrom ..other import unknown\n",
            "cycle_a.py": "import cycle_b\n", "cycle_b.py": "import cycle_a\n",
            "bad.py": "def broken(\n", "binary.py": "binary\x00content", "large.py": "x" * (dependencies.MAX_FILE_BYTES + 1),
            "unsafe.py": f"from pathlib import Path\nPath({str(self.root / 'executed')!r}).write_text('BAD')\nimport cfg\n",
            "vendor/hidden.py": "import cfg\n",
        }
        for name, text in files.items():
            target = self.work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.repo.index.add(list(files))
        self.repo.index.commit("dependency fixture", author=self.actor, committer=self.actor)
        context = patch.object(snapshots, "CLONE_DIR", str(self.root / "snapshots"))
        context.start()
        self.addCleanup(context.stop)
        self.snapshot = snapshots.create_snapshot(URL, str(self.work))

    def get(self, file_path="main.py", **kwargs):
        return get_file_dependencies(self.snapshot.root, URL, self.snapshot.commit_sha, file_path, **kwargs)

    def test_absolute_import_alias_and_from_member_locate_module_files(self):
        rows = self.get().references
        self.assertEqual((rows[0].module, rows[0].alias, rows[0].targets[0].file_path), ("cfg", "settings", "cfg.py"))
        row = next(row for row in rows if row.module == "pkg.mod" and row.kind == "from")
        self.assertEqual(row.targets[0].file_path, "pkg/mod.py")
        self.assertTrue(row.member_not_followed)

    def test_submodule_attribute_and_wildcard_uncertainty_are_preserved(self):
        rows = self.get().references
        sub = next(row for row in rows if row.imported_name == "sub")
        self.assertEqual(sub.resolution, "ambiguous_candidates")
        self.assertEqual({target.file_path for target in sub.targets}, {"pkg/__init__.py", "pkg/sub.py"})
        exported = next(row for row in rows if row.imported_name == "exported")
        self.assertEqual([target.file_path for target in exported.targets], ["pkg/__init__.py"])
        self.assertTrue(exported.member_not_followed)
        self.assertEqual(next(row for row in rows if row.imported_name == "*").resolution, "wildcard_not_expanded")

    def test_relative_imports_respect_explicit_source_root(self):
        default = self.get("src/spkg/a.py")
        self.assertEqual(default.references[0].resolution, "module_not_located")
        result = self.get("src/spkg/a.py", source_root="src")
        self.assertEqual(result.references[0].targets[0].file_path, "src/spkg/b.py")
        self.assertEqual(result.references[1].targets[0].file_path, "src/spkg/b.py")
        self.assertEqual(result.references[2].resolution, "relative_outside_source_root")
        self.assertEqual(self.get("pkg/__init__.py").references[0].targets[0].file_path, "pkg/mod.py")

    def test_namespace_submodule_and_duplicate_module_candidates(self):
        row = self.get("ns_user.py").references[0]
        self.assertEqual(row.resolution, "submodule_candidate")
        self.assertEqual(row.targets[0].file_path, "namespace/mod.py")
        row = self.get("conflict_user.py").references[0]
        self.assertEqual(row.resolution, "ambiguous_candidates")
        self.assertEqual(len(row.targets), 2)

    def test_dynamic_calls_are_not_resolved_and_context_is_preserved(self):
        rows = self.get().references
        dynamic = [row for row in rows if row.kind == "dynamic"]
        self.assertEqual(len(dynamic), 2)
        self.assertTrue(all(row.resolution == "dynamic_not_resolved" and not row.targets for row in dynamic))
        self.assertIn("If", next(row for row in rows if row.kind == "import" and row.module == "pkg.mod").context)
        self.assertIn("FunctionDef:lazy", next(row for row in rows if row.kind == "import" and row.module == "pkg.sub").context)

    def test_incoming_candidates_do_not_follow_reexports_or_dynamic_calls(self):
        result = self.get("pkg/mod.py", direction="incoming")
        self.assertIn("pkg/__init__.py", {row.source_file for row in result.references})
        self.assertFalse(any(row.imported_name == "exported" or row.kind == "dynamic" for row in result.references))
        self.assertGreater(result.unresolved_count, 0)
        self.assertTrue(all(any(target.file_path == "pkg/mod.py" for target in row.targets) for row in result.references))

    def test_circular_references_are_finite_direct_edges(self):
        outgoing = self.get("cycle_a.py")
        incoming = self.get("cycle_a.py", direction="incoming")
        self.assertEqual(outgoing.references[0].targets[0].file_path, "cycle_b.py")
        self.assertEqual(incoming.references[0].source_file, "cycle_b.py")

    def test_quotes_are_checked_and_pagination_has_no_duplicates(self):
        expected = self.get(limit=50).references
        rows, offset = [], 0
        while offset is not None:
            page = self.get(limit=2, offset=offset)
            rows.extend(page.references)
            offset = page.next_offset
        self.assertEqual(tuple(rows), expected)
        for row in rows:
            self.assertEqual(check_source_citation(self.snapshot.root, **row.evidence.model_dump()), row.evidence)
        self.assertTrue(dependency_payload(self.get())["references"][0]["evidence"]["url"].endswith("main.py#L1-L1"))

    def test_scan_limits_skips_and_no_execution_are_explicit(self):
        result = self.get("cfg.py", direction="incoming")
        reasons = {item.reason for item in result.skipped_examples}
        self.assertTrue({"python_parse_error", "binary_file", "file_too_large"}.issubset(reasons))
        self.assertNotIn("vendor/hidden.py", {row.source_file for row in result.references})
        self.assertFalse((self.root / "executed").exists())
        with patch.object(dependencies, "MAX_SCAN_FILES", 1):
            self.assertTrue(self.get("cfg.py", direction="incoming").scan_limited)
        with patch.object(dependencies, "MAX_SCAN_BYTES", 5):
            self.assertTrue(self.get().scan_limited)

    def test_unreadable_large_statement_does_not_claim_complete_evidence(self):
        text = "import module_" + "x" * dependencies.MAX_IMPORT_QUOTE_CHARS + "\n"
        (self.work / "statement.py").write_text(text)
        self.repo.index.add(["statement.py"])
        self.repo.index.commit("long import", author=self.actor, committer=self.actor)
        snapshot = snapshots.create_snapshot(URL, str(self.work))
        row = get_file_dependencies(snapshot.root, URL, snapshot.commit_sha, "statement.py").references[0]
        self.assertTrue(row.evidence_unavailable)
        self.assertIsNone(row.evidence)

    def test_input_scope_identity_tampering_and_symlink_checks(self):
        for changes in (dict(file_path="../outside.py"), dict(file_path="/etc/passwd"), dict(file_path="unknown.py"),
                        dict(source_root="../src"), dict(source_root="src"), dict(direction="both"), dict(limit=True), dict(offset=-1)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.get(**changes)
        with self.assertRaises(ValueError):
            get_file_dependencies(self.snapshot.root, URL, "0" * 40, "main.py")
        file = Path(self.snapshot.root) / "main.py"
        file.chmod(0o644)
        file.write_text("import TAMPERED\n")
        with self.assertRaisesRegex(SourceReadError, "changed"):
            self.get()
        file.unlink()
        file.symlink_to(self.work / "main.py")
        with self.assertRaisesRegex(SourceReadError, "Symbolic"):
            self.get()

    def test_old_version_and_untracked_files_do_not_mix(self):
        (self.work / "untracked.py").write_text("import cfg\n")
        (self.work / "main.py").write_text("import pkg.sub\n")
        self.repo.index.add(["main.py"])
        self.repo.index.commit("new imports", author=self.actor, committer=self.actor)
        snapshot = snapshots.create_snapshot(URL, str(self.work))
        self.assertEqual(self.get().references[0].module, "cfg")
        new = get_file_dependencies(snapshot.root, URL, snapshot.commit_sha, "main.py")
        self.assertEqual([row.module for row in new.references], ["pkg.sub"])
        incoming = get_file_dependencies(snapshot.root, URL, snapshot.commit_sha, "cfg.py", "incoming")
        self.assertNotIn("untracked.py", {row.source_file for row in incoming.references})

    def test_agent_and_mcp_share_results_and_framework_reads_before_citing(self):
        import mcp_server
        from agent import qa_agent
        functions = {}
        make_tools(SimpleNamespace(tool=lambda function: functions.setdefault(function.__name__, function)))
        deps = RepoDeps(URL, self.snapshot.root, "fixture", self.snapshot.commit_sha)
        raw = asyncio.run(functions["get_file_dependencies"](SimpleNamespace(deps=deps), "main.py"))
        with patch.object(mcp_server, "_current_repository", SimpleNamespace(commit_sha=self.snapshot.commit_sha)), patch.object(mcp_server, "_current_repo_root", self.snapshot.root), patch.object(mcp_server, "_current_repo_url", URL):
            self.assertEqual(json.loads(raw), mcp_server.get_file_dependencies("main.py"))
        self.assertFalse(deps.evidence.check_answer("Unregistered source [S1]").can_save)
        turns, results = [], []
        def model(messages, info):
            turns.append(len(messages))
            if len(turns) == 1:
                return ModelResponse(parts=[ToolCallPart("get_file_dependencies", {"file_path": "main.py"}, "deps")])
            if len(turns) == 2:
                returned = next(part for message in reversed(messages) for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "get_file_dependencies")
                source = json.loads(returned.content)["references"][0]["evidence"]
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": source["file_path"], "start_line": source["start_line"], "end_line": source["end_line"]}, "read")])
            return ModelResponse(parts=[TextPart("main.py imports cfg [S1]")])
        agent = Agent(FunctionModel(model), deps_type=RepoDeps)
        make_tools(agent)
        async def collect():
            return "".join([part async for part in qa_agent.ask_stream("Import candidates", URL, self.snapshot.root,
                collection_name="fixture", commit_sha=self.snapshot.commit_sha, on_result=results.append)])
        with patch.object(qa_agent, "_get_agent", return_value=agent), patch.object(qa_agent, "save_qa"):
            asyncio.run(collect())
        self.assertEqual(len(turns), 3)
        self.assertTrue(results[0].can_save)


if __name__ == "__main__":
    unittest.main()
