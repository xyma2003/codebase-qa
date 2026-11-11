import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from git import Actor, Repo
from pydantic_ai import Agent, models
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from streamlit.testing.v1 import AppTest

from agent import conversations, qa_agent
from agent.conversations import ConversationConflict, append_turn, create_conversation, load_conversation
from agent.evidence import CheckedAnswer
from agent.tools import RepoDeps, make_tools
from indexer import snapshots
from indexer.repository_service import RepositoryIndex

URL = "https://github.com/example/conversation-fixture"
COLLECTION = "fixture-collection"


class ConversationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.work = self.root / "work"
        repo = Repo.init(self.work)
        self.addCleanup(repo.close)
        (self.work / "alpha.py").write_text("def alpha():\n    return 1\n")
        (self.work / "beta.py").write_text("from alpha import alpha\n")
        repo.index.add(["alpha.py", "beta.py"])
        actor = Actor("Fixture", "fixture@example.invalid")
        repo.index.commit("history fixture", author=actor, committer=actor)
        for context in (patch.object(snapshots, "CLONE_DIR", str(self.root / "snapshots")),
                        patch.object(conversations, "CHROMA_DIR", str(self.root / "db")),
                        patch.object(models, "ALLOW_MODEL_REQUESTS", False)):
            context.start()
            self.addCleanup(context.stop)
        self.snapshot = snapshots.create_snapshot(URL, str(self.work))
        self.sha = self.snapshot.commit_sha
        self.session = create_conversation(URL, self.sha, COLLECTION)

    def load(self, session=None, **kwargs):
        return load_conversation(session or self.session, kwargs.get("repo_url", URL),
                                 kwargs.get("commit_sha", self.sha), kwargs.get("collection_name", COLLECTION))

    def messages(self, question="first", answer="answer"):
        return [ModelRequest(parts=[UserPromptPart(question)]), ModelResponse(parts=[TextPart(answer)])]

    def append(self, state=None, question="first", reference_count=0):
        append_turn(state or self.load(), question, self.messages(question), CheckedAnswer(text="answer"), reference_count)

    def ask(self, question, session=None):
        return asyncio.run(qa_agent.ask(question, URL, self.snapshot.root, collection_name=COLLECTION,
                                        commit_sha=self.sha, conversation_id=session or self.session))

    def test_native_messages_and_checked_answer_round_trip_without_duplication(self):
        source = RepoDeps(URL, self.snapshot.root, COLLECTION, self.sha).evidence
        source.read("alpha.py")
        checked = source.check_answer("alpha [S1]")
        messages = [ModelRequest(parts=[UserPromptPart("first")]),
                    ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py"}, "call-1")]),
                    ModelRequest(parts=[ToolReturnPart("read_file", "[S1] source", "call-1")]),
                    ModelResponse(parts=[TextPart("alpha [S1]")])]
        append_turn(self.load(), "first", messages, checked, 1)
        restored = self.load()
        self.assertEqual(restored.messages, messages)
        self.assertEqual(restored.turns[0].answer, checked)
        second_messages = self.messages("second")
        append_turn(restored, "second", second_messages, CheckedAnswer(text="answer"), 1)
        self.assertEqual(self.load().messages, messages + second_messages)
        self.assertEqual((self.load().revision, self.load().reference_count), (2, 1))

    def test_repository_version_and_collection_mismatch_are_rejected(self):
        for scope in ({"repo_url": "https://github.com/example/other"},
                      {"commit_sha": "a" * 40 if self.sha != "a" * 40 else "b" * 40},
                      {"collection_name": "other-index"}):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, "不一致"):
                self.load(**scope)

    def test_ids_and_unpinned_or_invalid_scopes_are_rejected(self):
        for session in ("../outside", str(uuid4())):
            with self.subTest(session=session), self.assertRaises(ValueError):
                self.load(session)
        for sha, collection in ((None, None), ("short", COLLECTION), (self.sha, "")):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                create_conversation(URL, sha, collection)

    def test_new_session_has_no_previous_context(self):
        self.append()
        other = create_conversation(URL, self.sha, COLLECTION)
        self.assertEqual(self.load(other).messages, [])
        self.assertEqual(self.load().revision, 1)

    def test_stale_completion_cannot_overwrite_or_duplicate_a_turn(self):
        first, concurrent = self.load(), self.load()
        self.append(first)
        with self.assertRaises(ConversationConflict):
            self.append(concurrent, "concurrent")
        self.assertEqual([turn.question for turn in self.load().turns], ["first"])

    def test_sqlite_insert_failure_rolls_back_revision_and_reference_counter(self):
        with conversations._connect() as db:
            db.execute("CREATE TRIGGER fail_turn BEFORE INSERT ON conversation_turns "
                       "BEGIN SELECT RAISE(ABORT, 'FIXTURE_WRITE_ERROR'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FIXTURE_WRITE_ERROR"):
            self.append(reference_count=3)
        self.assertEqual((self.load().revision, self.load().reference_count, self.load().turns), (0, 0, ()))

    def test_incomplete_corrupt_or_unknown_format_history_is_not_silently_reset(self):
        self.append()
        with conversations._connect() as db:
            db.execute("UPDATE conversation_turns SET messages_json='not-json'")
        with self.assertRaises(ValueError):
            self.load()
        with conversations._connect() as db:
            db.execute("DELETE FROM conversation_turns")
        with self.assertRaisesRegex(ValueError, "不完整"):
            self.load()
        with conversations._connect() as db:
            db.execute("UPDATE conversations SET format_version=99")
        with self.assertRaisesRegex(ValueError, "格式"):
            self.load()

    def test_invalid_append_inputs_leave_previous_state_unchanged(self):
        for count in (-1, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.append(reference_count=count)
        with self.assertRaises(ValueError):
            append_turn(self.load(), "", [], CheckedAnswer(text=""), 0)
        self.assertEqual(self.load().revision, 0)

    def make_agent(self, model):
        agent = Agent(FunctionModel(model), deps_type=RepoDeps)
        make_tools(agent)
        return agent

    def test_two_questions_receive_native_history_and_fresh_citations(self):
        observed = []
        def model(messages, info):
            prompts = [part.content for msg in messages for part in msg.parts if isinstance(part, UserPromptPart)]
            observed.append(prompts)
            second = prompts[-1] == "它在哪里被导入？"
            if second:
                self.assertEqual(prompts, ["alpha 是什么？", "它在哪里被导入？"])
                self.assertTrue(any(isinstance(part, ToolReturnPart) and "[S1]" in str(part.content)
                                    for msg in messages for part in msg.parts))
                self.assertTrue(any(isinstance(part, TextPart) and "alpha [S1]" in part.content
                                    for msg in messages for part in msg.parts))
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "beta.py" if second else "alpha.py"})])
            reference = "S2" if second else "S1"
            return ModelResponse(parts=[TextPart(f"{'import' if second else 'alpha'} [{reference}]")])
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), \
             patch.object(qa_agent, "evaluate", return_value=1), patch.object(qa_agent, "save_qa") as cache:
            self.assertIn("[S1](https://", self.ask("alpha 是什么？"))
            self.assertIn("[S2](https://", self.ask("它在哪里被导入？"))
        state = self.load()
        self.assertEqual((state.revision, state.reference_count), (2, 2))
        self.assertEqual([turn.answer.citations[0].file_path for turn in state.turns], ["alpha.py", "beta.py"])
        self.assertEqual(len(state.messages), 8)
        self.assertEqual(len(observed), 4)
        self.assertEqual(cache.call_count, 1)  # Follow-up cannot pollute standalone answer memory.

    def test_old_citation_is_rejected_even_after_a_new_file_read(self):
        def model(messages, info):
            prompts = [part.content for msg in messages for part in msg.parts if isinstance(part, UserPromptPart)]
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py" if len(prompts) == 1 else "beta.py"})])
            return ModelResponse(parts=[TextPart("copied old reference [S1]")])
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), patch.object(qa_agent, "save_qa"):
            self.ask("first")
            answer = self.ask("second")
        self.assertNotIn("https://", answer)
        self.assertEqual(self.load().turns[-1].answer.issues[0].reference_id, "S1")
        self.assertEqual(self.load().reference_count, 2)

    def test_uncited_but_issued_identifiers_are_not_reused_in_later_turns(self):
        def model(messages, info):
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py"})])
            return ModelResponse(parts=[TextPart("no source reference")])
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), patch.object(qa_agent, "save_qa"):
            self.ask("first")
        self.assertEqual(self.load().reference_count, 1)
        self.assertEqual(self.load().turns[0].answer.citations, ())

    def test_model_failure_leaves_history_unchanged_and_is_not_retried(self):
        self.append()
        before = self.load()
        def model(messages, info):
            raise RuntimeError("FIXTURE_MODEL_ERROR")
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)) as get_agent, \
             patch.object(qa_agent, "save_qa") as cache:
            with self.assertRaisesRegex(RuntimeError, "FIXTURE_MODEL_ERROR"):
                self.ask("failed follow-up")
            self.assertEqual(get_agent.call_count, 1)
            cache.assert_not_called()
        self.assertEqual(self.load(), before)

    def test_failure_after_tool_use_does_not_save_a_partial_turn(self):
        def model(messages, info):
            if isinstance(messages[-1].parts[-1], ToolReturnPart):
                raise RuntimeError("FIXTURE_FAILURE_AFTER_READ")
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py"})])
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), patch.object(qa_agent, "save_qa") as cache:
            with self.assertRaisesRegex(RuntimeError, "FIXTURE_FAILURE_AFTER_READ"):
                self.ask("first")
            cache.assert_not_called()
        self.assertEqual((self.load().revision, self.load().reference_count, self.load().messages), (0, 0, []))

    def test_storage_failure_does_not_publish_or_cache_a_successful_answer(self):
        results = []
        async def stream():
            return [part async for part in qa_agent.ask_stream(
                "first", URL, self.snapshot.root, collection_name=COLLECTION, commit_sha=self.sha,
                conversation_id=self.session, on_result=results.append)]
        model = lambda messages, info: ModelResponse(parts=[TextPart("local answer")])
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), \
             patch.object(qa_agent, "append_turn", side_effect=ConversationConflict("FIXTURE_CONFLICT")), \
             patch.object(qa_agent, "save_qa") as cache:
            with self.assertRaisesRegex(ConversationConflict, "FIXTURE_CONFLICT"):
                asyncio.run(stream())
            cache.assert_not_called()
        self.assertEqual(results, [])
        self.assertEqual(self.load().revision, 0)

    def test_wrong_version_is_rejected_before_calling_model(self):
        with patch.object(qa_agent, "_get_agent") as get_agent:
            with self.assertRaisesRegex(ValueError, "不一致"):
                asyncio.run(qa_agent.ask("question", URL, self.snapshot.root, collection_name="other",
                                          commit_sha=self.sha, conversation_id=self.session))
            get_agent.assert_not_called()

    def test_stream_and_ask_share_the_same_persisted_history(self):
        seen = []
        def model(messages, info):
            seen.append([part.content for msg in messages for part in msg.parts if isinstance(part, UserPromptPart)])
            return ModelResponse(parts=[TextPart("local answer")])
        results = []
        async def stream():
            return "".join([chunk async for chunk in qa_agent.ask_stream(
                "follow-up", URL, self.snapshot.root, collection_name=COLLECTION,
                commit_sha=self.sha, conversation_id=self.session, on_result=results.append)])
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), patch.object(qa_agent, "save_qa"):
            self.ask("first")
            self.assertEqual(asyncio.run(stream()), "local answer")
        self.assertEqual(seen, [["first"], ["first", "follow-up"]])
        self.assertEqual(results[0], self.load().turns[-1].answer)

    def test_streamlit_follow_up_uses_real_history_and_new_chat_starts_empty(self):
        # Streamlit/nest_asyncio patch asyncio globally. Isolate the integrated UI
        # check so later SDK tests retain normal event-loop/executor cleanup.
        if os.environ.get("CODEBASE_QA_UI_TEST_CHILD") != "1":
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_conversations.ConversationTests."
                 "test_streamlit_follow_up_uses_real_history_and_new_chat_starts_empty", "-v"],
                cwd=Path(__file__).resolve().parents[1],
                env=dict(os.environ, CODEBASE_QA_UI_TEST_CHILD="1"),
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            return
        observed = []
        def model(messages, info):
            prompts = [part.content for msg in messages for part in msg.parts if isinstance(part, UserPromptPart)]
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                observed.append(prompts)
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py"})])
            reference = "S2" if len(prompts) == 2 else "S1"
            return ModelResponse(parts=[TextPart(f"本地测试样例：alpha [{reference}]")])
        repository = RepositoryIndex(URL, self.sha, self.snapshot.root, COLLECTION, 2, 2, "2026-11-11", "fixture")
        with patch("indexer.repository_service.get_current_repository", return_value=repository), \
             patch("indexer.repository_service.list_repository_versions", return_value=[repository]), \
             patch("indexer.repository_service.get_repository_version", return_value=repository), \
             patch("memory.knowledge.get_memory_stats", return_value={"count": 0, "latest": None}), \
             patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), patch.object(qa_agent, "save_qa"):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=60)
            app.text_input[0].set_value(URL).run(timeout=60)
            app.chat_input[0].set_value("alpha 是什么？").run(timeout=60)
            session = app.session_state["conversation_id"]
            app.run(timeout=60)
            app.chat_input[0].set_value("它返回什么？").run(timeout=60)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.error), 0)
            self.assertEqual(self.load(session).revision, 2)
            self.assertEqual(len(app.session_state["messages"]), 4)
            self.assertEqual([item["sources"]["citations"][0]["reference_id"]
                              for item in app.session_state["messages"] if item["role"] == "assistant"], ["S1", "S2"])
            next(button for button in app.button if button.label == "新建对话").click().run(timeout=60)
            app.chat_input[0].set_value("new question").run(timeout=60)
            self.assertNotEqual(app.session_state["conversation_id"], session)
            self.assertEqual(self.load(session).revision, 2)
            self.assertEqual(len(app.exception), 0)
        self.assertEqual(observed, [["alpha 是什么？"], ["alpha 是什么？", "它返回什么？"], ["new question"]])


if __name__ == "__main__":
    unittest.main()
