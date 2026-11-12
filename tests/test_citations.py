from pydantic_ai.usage import RunUsage
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from git import Actor, Repo
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent.evidence import EvidenceStore
from agent.tools import RepoDeps, make_tools
from indexer import snapshots
from indexer.citations import check_source_citation
from indexer.source_reader import MAX_READ_CHARS, SourceReadError

URL = "https://github.com/example/fixture"


class CitationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = Repo.init(root / "work")
        self.addCleanup(repo.close)
        file = root / "work" / "example.py"
        file.write_text("def value():\n    return 1\n", encoding="utf-8")
        (root / "work" / "路径 [a]#.txt").write_text("引用原文\n")
        (root / "work" / "long.txt").write_text("first\n" + "x" * (MAX_READ_CHARS + 1))
        repo.index.add(["example.py", "路径 [a]#.txt", "long.txt"])
        actor = Actor("Fixture", "fixture@example.invalid")
        repo.index.commit("first", author=actor, committer=actor)
        with patch.object(snapshots, "CLONE_DIR", str(root / "snapshots")):
            self.old = snapshots.create_snapshot(URL, str(root / "work"))
            file.write_text("def value():\n    return 2\n")
            repo.index.add(["example.py"])
            repo.index.commit("second", author=actor, committer=actor)
            self.new = snapshots.create_snapshot(URL, str(root / "work"))
        self.store = EvidenceStore(URL, self.old.root, self.old.commit_sha)

    def check(self, **changes):
        arguments = dict(repo_root=self.old.root, repo_url=URL, commit_sha=self.old.commit_sha,
                         file_path="example.py", start_line=1, end_line=2,
                         source_text="def value():\n    return 1")
        return check_source_citation(**(arguments | changes))

    def test_exact_quote_and_fixed_commit_link(self):
        citation = self.check()
        self.assertEqual(citation.file_sha256, self.old.files["example.py"]["sha256"])
        self.assertEqual(citation.url, f"{URL}/blob/{self.old.commit_sha}/example.py#L1-L2")

    def test_non_ascii_and_special_paths_are_url_encoded(self):
        citation = self.check(file_path="路径 [a]#.txt", end_line=1, source_text="引用原文")
        self.assertIn("%E8%B7%AF%E5%BE%84%20%5Ba%5D%23.txt#L1-L1", citation.url)

    def test_wrong_version_repository_quote_hash_and_range_are_rejected(self):
        cases = [dict(commit_sha=self.new.commit_sha), dict(repo_url="https://github.com/example/other"),
                 dict(source_text="def value():\n    return 2"), dict(file_sha256="0" * 64),
                 dict(end_line=999), dict(start_line=0), dict(start_line=True), dict(end_line=1.5),
                 dict(file_path="../example.py"), dict(file_path="/etc/passwd"), dict(file_path="missing.py")]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises((ValueError, OSError)):
                self.check(**changes)

    def test_new_and_old_versions_are_independently_checked(self):
        self.check()
        result = self.check(repo_root=self.new.root, commit_sha=self.new.commit_sha,
                            source_text="def value():\n    return 2")
        self.assertEqual(result.commit_sha, self.new.commit_sha)

    def test_incomplete_lines_cannot_be_verified(self):
        with self.assertRaises(SourceReadError):
            self.check(file_path="long.txt", start_line=2, end_line=2, source_text="x" * MAX_READ_CHARS)
        output = self.store.read("long.txt", 2, 2)
        self.assertNotIn("[S1]", output)
        checked = self.store.check_answer("Complete giant line [S1]")
        self.assertFalse(checked.can_save)
        self.assertFalse(checked.citations)

    def test_only_complete_displayed_lines_receive_an_identifier(self):
        output = self.store.read("long.txt")
        self.assertIn("[S1]", output)
        checked = self.store.check_answer("First line [S1]")
        self.assertEqual(checked.citations[0].source_text, "first")
        self.assertEqual(checked.citations[0].end_line, 1)

    def test_repeated_reads_deduplicate_and_links_are_program_generated(self):
        first = self.store.read("example.py", 1, 2)
        self.assertEqual(self.store.read("example.py", 1, 2), first)
        checked = self.store.check_answer("Value is one [S1](https://untrusted.invalid). Again [S1].")
        self.assertTrue(checked.can_save)
        self.assertEqual(len(checked.citations), 1)
        self.assertNotIn("untrusted.invalid", checked.text)
        self.assertEqual(checked.text.count(checked.citations[0].url), 2)

    def test_unknown_or_missing_identifiers_are_not_accepted(self):
        self.store.read("example.py")
        checked = self.store.check_answer("Known [S1], invented [S99](https://untrusted.invalid).")
        self.assertFalse(checked.can_save)
        self.assertEqual(checked.issues[0].reference_id, "S99")
        self.assertNotIn("untrusted.invalid", checked.text)
        self.assertFalse(self.store.check_answer("No source evidence.").can_save)

    def test_tokens_in_code_and_escaped_examples_are_not_citations(self):
        self.store.read("example.py")
        answer = "`[S99]`\n```python\n[S98]\n```\n~~~\n[S97]\n~~~\n\\[S96]\nReal [S1]"
        checked = self.store.check_answer(answer)
        self.assertTrue(checked.can_save)
        self.assertEqual(len(checked.citations), 1)
        self.assertIn("`[S99]`", checked.text)

    def test_new_question_has_a_separate_evidence_store(self):
        self.store.read("example.py")
        other = EvidenceStore(URL, self.old.root, self.old.commit_sha)
        self.assertFalse(other.check_answer("Previous answer [S1]").can_save)

    def test_source_tampering_between_read_and_answer_is_rejected(self):
        self.store.read("example.py")
        file = Path(self.old.root) / "example.py"
        file.chmod(0o644)
        file.write_text("def value():\n    return 999\n")
        checked = self.store.check_answer("Old evidence [S1]")
        self.assertFalse(checked.citations)
        self.assertFalse(checked.can_save)
        self.assertIn("无法核对", checked.issues[0].message)

    def test_real_framework_tool_loop_builds_and_checks_citations(self):
        from agent import qa_agent
        calls = []
        def model(messages, info):
            self.assertIn("cite source evidence", info.instructions)
            calls.append(messages)
            if len(calls) == 1:
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "example.py", "start_line": 1, "end_line": 2}, "fixture-call")])
            return ModelResponse(parts=[TextPart("value returns one in example.py [S1]")])
        agent = Agent(FunctionModel(model), deps_type=RepoDeps)
        make_tools(agent)
        results = []
        async def collect():
            return "".join([part async for part in qa_agent.ask_stream(
                "How does value work?", URL, self.old.root,
                collection_name="fixture", commit_sha=self.old.commit_sha, on_result=results.append,
            )])
        with patch.object(qa_agent, "_get_agent", return_value=agent), patch.object(qa_agent, "evaluate", return_value=1), patch.object(qa_agent, "save_qa") as save:
            answer = asyncio.run(collect())
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].can_save)
        self.assertIn(f"/blob/{self.old.commit_sha}/example.py#L1-L2", answer)
        self.assertEqual(save.call_args.args[1], answer)
        self.assertEqual(save.call_args.kwargs["commit_sha"], self.old.commit_sha)

    def test_invalid_or_missing_citations_are_not_saved_even_with_high_score(self):
        from agent import qa_agent
        for answer in ("Made-up source [S99]", "No source"):
            run = AsyncMock(return_value=SimpleNamespace(output=answer, usage=RunUsage, new_messages=lambda answer=answer: [ModelResponse(parts=[TextPart(answer)])]))
            with self.subTest(answer=answer), patch.object(qa_agent, "_get_agent", return_value=SimpleNamespace(run=run)), patch.object(qa_agent, "evaluate", return_value=1), patch.object(qa_agent, "save_qa") as save:
                asyncio.run(qa_agent.ask("question", URL, self.old.root, collection_name="fixture", commit_sha=self.old.commit_sha))
                save.assert_not_called()

    def test_legacy_unpinned_answer_behavior_is_preserved(self):
        from agent import qa_agent
        run = AsyncMock(return_value=SimpleNamespace(output="original answer", usage=RunUsage, new_messages=lambda: [ModelResponse(parts=[TextPart("original answer")])]))
        with patch.object(qa_agent, "_get_agent", return_value=SimpleNamespace(run=run)), patch.object(qa_agent, "evaluate", return_value=1), patch.object(qa_agent, "save_qa") as save:
            self.assertEqual(asyncio.run(qa_agent.ask("question", URL, self.old.root)), "original answer")
            self.assertNotIn("instructions", run.call_args.kwargs)
            save.assert_called_once()

    def test_pinned_search_reads_real_source_and_rejects_cross_version_results(self):
        functions = {}
        make_tools(SimpleNamespace(tool=lambda f: functions.setdefault(f.__name__, f)))
        deps = RepoDeps(URL, self.old.root, "fixture", self.old.commit_sha)
        result = dict(file_path="example.py", start_line=1, end_line=2, content="UNTRUSTED_INDEX_BODY", commit_sha=self.old.commit_sha)
        with patch("agent.tools.search_memory", return_value=None), patch("agent.tools.search", return_value=[result]):
            text = asyncio.run(functions["search_code"](SimpleNamespace(deps=deps), "value"))
        self.assertIn("return 1", text)
        self.assertNotIn("UNTRUSTED_INDEX_BODY", text)
        result["commit_sha"] = self.new.commit_sha
        with patch("agent.tools.search_memory", return_value=None), patch("agent.tools.search", return_value=[result]):
            text = asyncio.run(functions["search_code"](SimpleNamespace(deps=deps), "value"))
        self.assertIn("another code version", text)

    def test_cached_answer_does_not_register_source_evidence(self):
        functions = {}
        make_tools(SimpleNamespace(tool=lambda f: functions.setdefault(f.__name__, f)))
        deps = RepoDeps(URL, self.old.root, "fixture", self.old.commit_sha)
        hit = dict(answer="Old answer [S1]", score=1, hit_count=1)
        with patch("agent.tools.search_memory", return_value=hit), patch("agent.tools.search") as search:
            text = asyncio.run(functions["search_code"](SimpleNamespace(deps=deps), "value"))
        search.assert_not_called()
        self.assertIn("Cached answer only", text)
        self.assertFalse(deps.evidence.check_answer("Copied answer [S1]").can_save)


if __name__ == "__main__":
    unittest.main()
