from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from indexer.repository_service import RepositoryIndex
from agent.evidence import CheckedAnswer, CitedSource, CitationIssue

ROOT = Path(__file__).resolve().parents[1]
URL = "https://github.com/example/fixture"


class RepositoryUITests(unittest.TestCase):
    def setUp(self):
        def version(sha, collection):
            return RepositoryIndex(URL, sha, "fixture-root", collection, 1, 1, "2026-11-10", "fixture")
        self.old = version("a" * 40, "old-collection")
        self.new = version("b" * 40, "new-collection")
        contexts = [
            patch("indexer.repository_service.get_current_repository", side_effect=lambda url: self.new if url == URL else None),
            patch("indexer.repository_service.list_repository_versions", return_value=[self.new, self.old]),
            patch("indexer.repository_service.get_repository_version", side_effect=lambda url, sha: self.old if sha == self.old.commit_sha else self.new),
            patch("memory.knowledge.get_memory_stats", return_value={"count": 0, "latest": None}),
        ]
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)
        self.app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=60)
        self.app.text_input[0].set_value(URL).run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)

    def test_version_selection_binds_the_matching_collection_and_clears_chat(self):
        self.app.session_state["messages"] = [{"role": "assistant", "content": "previous version"}]
        self.app.selectbox[0].select(self.old.commit_sha).run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)
        selected = self.app.session_state["repository_index"]
        self.assertEqual((selected.commit_sha, selected.collection_name), (self.old.commit_sha, "old-collection"))
        self.assertEqual(self.app.session_state["messages"], [])

    def test_changing_repository_clears_the_old_context(self):
        self.app.text_input[0].set_value("https://github.com/example/other").run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)
        self.assertNotIn("repository_index", self.app.session_state)
        self.assertNotIn("repo_url", self.app.session_state)

    def test_failed_refresh_keeps_the_displayed_version(self):
        with patch("indexer.repository_service.import_repository", side_effect=RuntimeError("FIXTURE_REFRESH_ERROR")):
            self.app.button[0].click().run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.app.session_state["repository_index"].commit_sha, self.new.commit_sha)
        self.assertIn("FIXTURE_REFRESH_ERROR", self.app.error[0].value)

    def test_refresh_can_return_from_an_old_selection_to_the_current_version(self):
        self.app.selectbox[0].select(self.old.commit_sha).run(timeout=60)
        with patch("indexer.repository_service.import_repository", return_value=self.new):
            self.app.button[0].click().run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.app.session_state["repository_index"].commit_sha, self.new.commit_sha)
        self.assertEqual(self.app.selectbox[0].value, self.new.commit_sha)

    def test_answer_sources_are_rendered_and_retained_on_rerun(self):
        source = CitedSource(
            reference_id="S1", repo_url=URL, commit_sha=self.new.commit_sha,
            file_path="src/[example].py", start_line=4, end_line=5,
            source_text="def value():\n    return 1", file_sha256="c" * 64,
        )
        checked = CheckedAnswer(text=f"测试回答 [S1]({source.url})", citations=(source,))
        async def response(*args, on_result=None, **kwargs):
            on_result(checked)
            yield checked.text
        with patch("agent.qa_agent.ask_stream", side_effect=response):
            self.app.chat_input[0].set_value("测试问题").run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.app.code[0].value, "4 | def value():\n5 |     return 1")
        self.assertIn(source.url, self.app.markdown[-1].value)
        self.assertIn(r"src/\[example\].py", self.app.markdown[-1].value)
        self.app.run(timeout=60)
        self.assertEqual(self.app.code[0].value, source.numbered_source)
        self.assertEqual(self.app.session_state["messages"][-1]["sources"]["citations"][0]["commit_sha"], self.new.commit_sha)

    def test_invalid_reference_is_visible_without_a_fake_source_link(self):
        checked = CheckedAnswer(text="测试回答 [S99]", issues=(CitationIssue(reference_id="S99", message="引用编号不存在。"),))
        async def response(*args, on_result=None, **kwargs):
            on_result(checked)
            yield checked.text
        with patch("agent.qa_agent.ask_stream", side_effect=response):
            self.app.chat_input[0].set_value("测试问题").run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)
        self.assertIn("S99：引用编号不存在。", self.app.markdown[-1].value)
        self.assertEqual(len(self.app.code), 0)


if __name__ == "__main__":
    unittest.main()
