import asyncio
import json
from types import SimpleNamespace
import unittest

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from agent import conversations, qa_agent
from agent.tools import RepoDeps
from agent.tracing import traced_tool
from tests import test_conversations as fixtures


class HistoryToolTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ConversationTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.events = []

    async def model(self, messages, info):
        latest = max(number for number, message in enumerate(messages)
                     if any(isinstance(part, UserPromptPart) for part in message.parts))
        returns = [part for message in messages[latest:] for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py"})])
        import re
        reference = re.search(r"\[(S\d+)\]", returns[-1].content)[1]
        return ModelResponse(parts=[TextPart(f"本地测试回答 [{reference}]")])

    def run_question(self, question="本地问题", observer=None):
        deps = RepoDeps(fixtures.URL, self.helper.snapshot.root, fixtures.COLLECTION, self.helper.sha, source_only=True)
        return asyncio.run(qa_agent._run_answer(question, deps, self.helper.session,
            agent=qa_agent.build_agent(FunctionModel(self.model)), on_event=observer or self.events.append))

    def test_live_events_and_persisted_checked_answer_have_actual_tool_data(self):
        answer = self.run_question()
        self.assertEqual([event.status for event in self.events], ["running", "returned"])
        self.assertEqual(len(answer.tool_events), 1)
        event = answer.tool_events[0]
        self.assertEqual((event.tool_name, event.arguments["file_path"]), ("read_file", "alpha.py"))
        self.assertGreaterEqual(event.elapsed_ms, 0)
        self.assertGreater(event.output_chars, 0)
        self.assertEqual(self.helper.load().turns[0].answer.tool_events, answer.tool_events)

    def test_follow_up_keeps_separate_events_and_fresh_reference_ids(self):
        first = self.run_question()
        second = self.run_question("继续本地问题")
        self.assertEqual(len(second.tool_events), 1)
        self.assertNotEqual(first.tool_events[0].call_id, second.tool_events[0].call_id)
        self.assertEqual([citation.reference_id for citation in second.citations], ["S2"])
        self.assertEqual(len(self.helper.load().turns), 2)

    def test_listing_filters_repository_commit_collection_and_excludes_empty(self):
        self.assertEqual(conversations.list_conversations(fixtures.URL, self.helper.sha, fixtures.COLLECTION), [])
        self.run_question()
        listed = conversations.list_conversations(fixtures.URL, self.helper.sha, fixtures.COLLECTION)
        self.assertEqual(listed, [{"conversation_id": self.helper.session, "turn_count": 1, "title": "本地问题"}])
        for scope in (("https://github.com/example/other", self.helper.sha, fixtures.COLLECTION),
                      (fixtures.URL, "a" * 40, fixtures.COLLECTION), (fixtures.URL, self.helper.sha, "other-collection")):
            self.assertEqual(conversations.list_conversations(*scope), [])

    def test_restored_messages_include_checked_sources_and_events(self):
        checked = self.run_question()
        messages = conversations.display_messages(self.helper.load())
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["sources"], checked.model_dump(mode="json"))

    def test_older_saved_answers_without_events_still_load(self):
        self.run_question()
        with conversations._connect() as db:
            raw = db.execute("SELECT answer_json FROM conversation_turns").fetchone()[0]
            value = json.loads(raw)
            value.pop("tool_events")
            db.execute("UPDATE conversation_turns SET answer_json=?", (json.dumps(value),))
        self.assertEqual(self.helper.load().turns[0].answer.tool_events, ())

    def test_display_callback_failure_does_not_change_answer_or_tool_record(self):
        def broken(event):
            raise RuntimeError("DISPLAY_FAILURE")
        checked = self.run_question(observer=broken)
        self.assertTrue(checked.can_save)
        self.assertEqual(checked.tool_events[0].status, "returned")

    def test_failed_tool_reports_failure_and_returned_error_is_not_called_success(self):
        deps = SimpleNamespace(tool_events=[], tool_observer=self.events.append)
        @traced_tool
        async def fails(ctx, value):
            raise ValueError("ORIGINAL_TOOL_FAILURE")
        with self.assertRaisesRegex(ValueError, "ORIGINAL_TOOL_FAILURE"):
            asyncio.run(fails(SimpleNamespace(deps=deps), 1))
        self.assertEqual([event.status for event in self.events], ["running", "failed"])
        self.assertEqual(deps.tool_events[0].arguments, {"value": 1})


if __name__ == "__main__":
    unittest.main()
