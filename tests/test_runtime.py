import asyncio
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from agent import qa_agent
from agent.evidence import CheckedAnswer
from agent.runtime import RunPolicy
from agent.tools import RepoDeps
from tests import test_history_tools as fixtures


class RuntimePolicyTests(unittest.TestCase):
    def test_policy_rejects_invalid_values_without_converting_types(self):
        for settings in ({"request_limit": 0}, {"tool_calls_limit": -1}, {"total_tokens_limit": 0},
                         {"timeout_seconds": float("inf")}, {"timeout_seconds": 0}, {"input_characters_limit": True},
                         {"request_limit": "2"}, {"unknown": 1}):
            with self.subTest(settings=settings), self.assertRaises(ValidationError):
                RunPolicy(**settings)
        self.assertEqual(RunPolicy().usage_limits().request_limit, 50)
        self.assertIsNone(CheckedAnswer.model_validate({"text": "old answer"}).runtime)
        with self.assertRaisesRegex(ValueError, "boolean"):
            RepoDeps("url", "root", use_answer_cache="false")


class RuntimeTurnTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.HistoryToolTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.base = self.helper.helper

    def deps(self, **kwargs):
        return RepoDeps(fixtures.fixtures.URL, self.base.snapshot.root, fixtures.fixtures.COLLECTION, self.base.sha,
                        use_answer_cache=False, **kwargs)

    def run_turn(self, model=None, policy=None, **kwargs):
        return asyncio.run(qa_agent._run_answer("本地运行测试", self.deps(source_only=True), self.base.session,
            agent=qa_agent.build_agent(FunctionModel(model or self.helper.model)), policy=policy, **kwargs))

    def test_limits_and_actual_usage_survive_saved_conversation(self):
        policy = RunPolicy(request_limit=3, tool_calls_limit=1, total_tokens_limit=5000,
                           timeout_seconds=5.0, input_characters_limit=10000)
        answer = self.run_turn(policy=policy)
        self.assertEqual((answer.runtime.requests, answer.runtime.tool_calls), (2, 1))
        self.assertEqual(answer.runtime.policy, policy)
        self.assertEqual(self.base.load().turns[-1].answer.runtime, answer.runtime)
        self.assertTrue(answer.can_save)

    def test_request_and_tool_limits_stop_without_advancing_history(self):
        for policy in (RunPolicy(request_limit=1), RunPolicy(tool_calls_limit=0)):
            with self.subTest(policy=policy), self.assertRaises(UsageLimitExceeded):
                self.run_turn(policy=policy)
            self.assertEqual(self.base.load().revision, 0)

    def test_reported_token_limit_stops_without_saving_partial_answer(self):
        calls = []
        def response(messages, info):
            calls.append(1)
            return ModelResponse(parts=[TextPart("本地模拟用量")], usage=RequestUsage(input_tokens=20, output_tokens=10))
        with self.assertRaises(UsageLimitExceeded):
            self.run_turn(response, RunPolicy(total_tokens_limit=5))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.base.load().revision, 0)

    def test_timeout_cancels_pending_turn_and_preserves_saved_history(self):
        self.run_turn()
        calls, cancelled = [], []
        async def pending(messages, info):
            calls.append(1)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(1)
        with self.assertRaisesRegex(TimeoutError, "已停止等待"):
            self.run_turn(pending, RunPolicy(timeout_seconds=0.05))
        self.assertEqual((len(calls), len(cancelled)), (1, 1))
        self.assertEqual(self.base.load().revision, 1)

    def test_input_guard_runs_before_model_creation_without_truncating_history(self):
        self.run_turn()
        before = self.base.load()
        with patch("agent.qa_agent._get_agent", side_effect=AssertionError("MODEL_MUST_NOT_START")):
            with self.assertRaisesRegex(ValueError, "超过设置"):
                asyncio.run(qa_agent._run_answer("继续", self.deps(source_only=True), self.base.session,
                                               policy=RunPolicy(input_characters_limit=5)))
        after = self.base.load()
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.messages, before.messages)

    def test_cache_off_public_interfaces_skip_both_reads_and_writes(self):
        captured = []
        async def main():
            agent = qa_agent.build_agent(FunctionModel(self.helper.model))
            with patch("agent.qa_agent._get_agent", return_value=agent), \
                 patch("agent.tools.search_memory", side_effect=AssertionError("NO_CACHE_READ")), \
                 patch("agent.qa_agent.save_qa") as saved, patch("agent.qa_agent.evaluate", return_value=1):
                text = await qa_agent.ask("本地接口测试", fixtures.fixtures.URL, self.base.snapshot.root,
                    collection_name=fixtures.fixtures.COLLECTION, commit_sha=self.base.sha, use_answer_cache=False)
                async for _ in qa_agent.ask_stream("本地接口测试", fixtures.fixtures.URL, self.base.snapshot.root,
                    collection_name=fixtures.fixtures.COLLECTION, commit_sha=self.base.sha, use_answer_cache=False,
                    on_result=captured.append):
                    pass
                saved.assert_not_called()
                return text
        self.assertIn("本地测试回答", asyncio.run(main()))
        self.assertFalse(captured[0].runtime.use_answer_cache)
        self.assertIsNone(captured[0].runtime.policy)

    def test_default_policy_does_not_override_sdk_options(self):
        agent = qa_agent.build_agent(FunctionModel(self.helper.model))
        original = agent.run
        with patch.object(agent, "run", wraps=original) as run:
            asyncio.run(qa_agent._run_answer("默认选项", self.deps(source_only=True), agent=agent))
        self.assertNotIn("usage_limits", run.call_args.kwargs)
        self.assertNotIn("model_settings", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
