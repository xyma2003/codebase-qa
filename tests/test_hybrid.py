import asyncio
import re
from unittest.mock import patch
import unittest

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agent import qa_agent
from agent.tools import RepoDeps
from indexer.hybrid import code_tokens, fuse_ranks, hybrid_search
from tests import test_retrieval as fixtures


class HybridRankTests(unittest.TestCase):
    def test_code_tokens_work_next_to_chinese_and_ignore_plain_words(self):
        self.assertEqual(code_tokens("请定位read_file，比较config.py以及RepoDeps"), ["read_file", "config.py", "RepoDeps"])
        self.assertEqual(code_tokens("这是什么项目，请解释一下"), [])
        self.assertLessEqual(len(code_tokens(" ".join(f"name_{i}" for i in range(20)))), 8)

    def test_fusion_deduplicates_per_channel_and_rewards_shared_sources(self):
        hit = lambda name, line: {"file_path": name, "start_line": line, "end_line": line}
        shared, dense, lexical = hit("shared.py", 1), hit("dense.py", 1), hit("lexical.py", 1)
        results = fuse_ranks([dense, shared, shared], [lexical, shared])
        self.assertEqual(results[0]["file_path"], "shared.py")
        self.assertEqual(results[0]["retrieval_channels"], ["semantic", "lexical"])
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["fusion_score"], 2/62)


class HybridSourceTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.RetrievalIndexTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.fixture = self.helper.fixture
        from evaluation.dataset import read_json
        self.source = read_json((self.helper.indexes / "index.json").read_bytes())["sources"][0]

    def search(self, query):
        source = self.source
        return hybrid_search(source["repo_root"], source["repo_url"], source["commit_sha"], query, source["collection_name"])

    def test_path_and_symbol_results_use_actual_source_ranges(self):
        paths = self.search("定位sample.py的定义")
        self.assertTrue(any(hit["start_line"] == 1 and "lexical" in hit["retrieval_channels"] for hit in paths))
        symbols = self.search("alpha在哪里")  # Lowercase plain words remain semantic-only.
        self.assertTrue(all(hit["retrieval_channels"] == ["semantic"] for hit in symbols))
        self.assertFalse((self.fixture.root / "executed").exists())

    def test_wrong_source_and_unpinned_hybrid_are_rejected(self):
        source = self.source
        with self.assertRaisesRegex(ValueError, "scope mismatch"):
            hybrid_search(source["repo_root"], source["repo_url"], "a"*40, "sample.py", source["collection_name"])
        with self.assertRaisesRegex(ValueError, "pinned"):
            RepoDeps(source["repo_url"], source["repo_root"], retrieval_strategy="hybrid")

    def test_agent_hybrid_tool_mints_checked_identifiers_and_respects_cache_switch(self):
        async def respond(messages, info):
            returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            if not returns:
                return ModelResponse(parts=[ToolCallPart("search_code", {"query": "sample.py"})])
            reference = re.search(r"\[(S\d+)\]", returns[-1].content)[1]
            return ModelResponse(parts=[TextPart(f"本地融合检索样例 [{reference}]")])
        source = self.source
        deps = RepoDeps(source["repo_url"], source["repo_root"], source["collection_name"], source["commit_sha"],
                        retrieval_strategy="hybrid", use_answer_cache=False)
        with patch("agent.tools.search_memory", side_effect=AssertionError("CACHE_MUST_NOT_RUN")), patch("agent.qa_agent.save_qa") as saved:
            checked = asyncio.run(qa_agent._run_answer("sample.py", deps, agent=qa_agent.build_agent(FunctionModel(respond))))
        saved.assert_not_called()
        self.assertTrue(checked.can_save)
        self.assertEqual(checked.citations[0].commit_sha, source["commit_sha"])

    def test_mcp_exposes_opt_in_fusion_without_changing_default_tools(self):
        import mcp_server
        source = self.source
        from types import SimpleNamespace
        with patch.object(mcp_server, "_current_repository", SimpleNamespace(commit_sha=source["commit_sha"], collection_name=source["collection_name"])), \
             patch.object(mcp_server, "_current_repo_url", source["repo_url"]), patch.object(mcp_server, "_current_repo_root", source["repo_root"]), \
             patch.object(mcp_server, "search_memory", side_effect=AssertionError("CACHE_MUST_NOT_RUN")):
            result = mcp_server.search_code("sample.py", retrieval_strategy="hybrid", use_answer_cache=False)
        self.assertIn("sample.py", result)


if __name__ == "__main__":
    unittest.main()
