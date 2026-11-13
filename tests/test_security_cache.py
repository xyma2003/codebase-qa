import asyncio
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import chromadb
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from streamlit.testing.v1 import AppTest

from agent import qa_agent
from agent.evidence import EvidenceStore
from agent.tools import RepoDeps
from memory import knowledge as cache
from indexer.repository_service import RepositoryIndex
from tests import test_citations as fixtures
from tests.test_real_evaluation import FixtureEmbedding


class CachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CitationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.snapshot = self.fixture.old
        self.url, self.sha = self.snapshot.repo_url, self.snapshot.commit_sha
        self.db = Path(self.snapshot.root).parents[3]/"cache-db"
        self.client = chromadb.PersistentClient(path=str(self.db))
        for context in (patch.object(cache, "CHROMA_DIR", str(self.db)),
                        patch.object(cache, "_get_client", return_value=self.client),
                        patch.object(cache, "_get_embed_fn", side_effect=FixtureEmbedding)):
            context.start()
            self.addCleanup(context.stop)
        evidence = EvidenceStore(self.url, self.snapshot.root, self.sha)
        evidence.read("example.py", 1, 2)
        self.answer = evidence.check_answer("本地缓存样例：example.py 的返回值为 1 [S1]。")

    def save(self, **kwargs):
        return cache.save_qa("本地缓存审核测试", self.answer.text, self.url, commit_sha=self.sha,
                             citations=self.answer.citations, **kwargs)

    def rows(self):
        return cache.list_cache(self.url, self.sha)

    def search(self):
        return cache.search_memory("本地缓存审核测试", self.url, commit_sha=self.sha, repo_root=self.snapshot.root)

    def review(self, row, approve=True, root=None):
        cache.review_qa(self.url, self.sha, root or self.snapshot.root, row["id"], row["review_token"], approve=approve)

    def test_pending_and_legacy_rows_are_not_retrieved_or_embedded_as_query(self):
        identity = self.save()
        self.assertEqual(self.rows()[0]["metadata"]["approval_status"], "pending")
        with patch.object(cache, "_get_embed_fn", side_effect=AssertionError("NO_PENDING_QUERY")):
            self.assertIsNone(self.search())
        collection = cache._collection(self.url, self.sha)
        collection.add(ids=["legacy-fixture"], documents=["unreviewed legacy"], embeddings=[[1., 0., .5]],
                       metadatas=[{"question": "legacy", "answer": "legacy", "approval_status": "approved"}])
        # A forged status alone cannot establish a reviewed, source-bound answer.
        self.assertIsNone(self.search())
        legacy = next(row for row in self.rows() if row["id"] == "legacy-fixture")
        with self.assertRaises(ValueError):
            self.review(legacy)

    def test_approved_answer_is_rechecked_then_revocation_stops_reuse(self):
        self.save()
        self.review(self.rows()[0])
        hit = self.search()
        self.assertEqual(hit["answer"], self.answer.text)
        self.assertEqual(hit["approval_status"], "approved")
        self.review(self.rows()[0], approve=False)
        self.assertIsNone(self.search())
        self.assertEqual(self.rows()[0]["metadata"]["approval_status"], "revoked")

    def test_changed_document_and_changed_source_are_not_accepted(self):
        identity = self.save()
        self.review(self.rows()[0])
        collection = cache._collection(self.url, self.sha)
        collection.update(ids=[identity], documents=["forged cached output"], embeddings=[[1., 0., .5]])
        self.assertIsNone(self.search())
        with self.assertRaises(ValueError):
            self.review(self.rows()[0])
        # Restoring the original document does not bypass source readback.
        collection.update(ids=[identity], documents=[f"Q: 本地缓存审核测试\nA: {self.answer.text}"], embeddings=[[1., 0., .5]])
        source = Path(self.snapshot.root)/"example.py"
        source.chmod(0o644)
        source.write_text("def value():\n    return 9\n")
        self.assertIsNone(self.search())

    def test_wrong_source_and_stale_review_cannot_promote_or_revert_an_answer(self):
        self.save()
        row = self.rows()[0]
        with self.assertRaises(ValueError):
            self.review(row, root=self.fixture.new.root)
        self.review(row)
        with self.assertRaisesRegex(ValueError, "已被修改"):
            self.review(row, approve=False)
        self.assertEqual(self.rows()[0]["metadata"]["approval_status"], "approved")

    def test_new_agent_answer_remains_pending_and_agent_has_no_approval_tool(self):
        def respond(messages, info):
            self.assertFalse(any("approv" in tool.name or "review" in tool.name for tool in info.function_tools))
            if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "example.py", "start_line": 1, "end_line": 2})])
            return ModelResponse(parts=[TextPart("本地缓存样例：example.py [S1]。")])
        deps = RepoDeps(self.url, self.snapshot.root, "fixture", self.sha)
        with patch.object(qa_agent, "evaluate", return_value=1):
            result = asyncio.run(qa_agent._run_answer("本地问题", deps, agent=qa_agent.build_agent(FunctionModel(respond))))
        self.assertTrue(result.can_save)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["metadata"]["approval_status"], "pending")
        self.assertIsNone(self.search())

    def test_cache_write_requires_sources_and_rejects_credentials_before_storage(self):
        with patch.object(cache, "_get_client", side_effect=AssertionError("STORAGE_MUST_NOT_RUN")):
            with self.assertRaises(ValueError):
                cache.save_qa("q", "a", self.url)
            with self.assertRaises(ValueError):
                cache.save_qa("q", "sk-"+"A"*32, self.url, commit_sha=self.sha, citations=self.answer.citations)


class CacheReviewUITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CODEBASE_QA_LOCAL_MODE":"1"}))
        self.helper = CachePolicyTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.helper.save()
        self.app = AppTest.from_string("from memory.review_ui import render_cache_review\nrender_cache_review()")
        self.app.session_state["repository_index"] = RepositoryIndex(self.helper.url, self.helper.sha,
            self.helper.snapshot.root, "fixture", 1, 1, "fixture", "fixture")
        self.app.run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)

    def button(self, label):
        return next(button for button in self.app.button if button.label == label)

    def test_review_and_revoke_are_explicit_user_actions(self):
        self.assertIsNone(self.helper.search())
        self.button("批准使用").click().run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)
        self.assertIsNotNone(self.helper.search())
        self.button("撤销批准").click().run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)
        self.assertIsNone(self.helper.search())

    def test_old_rendered_button_cannot_approve_modified_content(self):
        collection = cache._collection(self.helper.url, self.helper.sha)
        identity = self.helper.rows()[0]["id"]
        collection.update(ids=[identity], documents=["modified after display"], embeddings=[[1., 0., .5]])
        self.button("批准使用").click().run(timeout=60)
        self.assertEqual(len(self.app.exception), 0)
        self.assertTrue(any("已被修改" in item.value for item in self.app.error))
        self.assertIsNone(self.helper.search())
