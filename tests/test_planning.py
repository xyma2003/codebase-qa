import asyncio
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from git import Actor, Repo
from pydantic import ValidationError
from pydantic_ai import Agent, models
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from streamlit.testing.v1 import AppTest

from agent import conversations, qa_agent
from agent.conversations import create_conversation, load_conversation
from agent.evidence import CheckedAnswer, EvidenceStore
from agent.planning import ChangePlan, check_plan, render_plan
from agent.tools import RepoDeps, make_tools
from indexer import snapshots
from indexer.repository_service import RepositoryIndex

URL = "https://github.com/example/planning-fixture"
COLLECTION = "planning-fixture"


def proposal(reference="S1"):
    return {
        "goal": "让 alpha 支持输入值",
        "current_behavior": [{"description": "alpha 固定返回 1", "references": [reference]}],
        "file_changes": [
            {"file_path": "alpha.py", "action": "modify", "reason": "为调用方提供输入参数",
             "changes": ["增加 value 参数", "返回 value"], "references": [reference]},
            {"file_path": "tests/test_alpha.py", "action": "create", "reason": "覆盖 alpha 的参数行为",
             "changes": ["检查默认值与传入值"], "references": [reference]},
        ],
        "steps": [{"description": "调整 alpha 接口", "file_paths": ["alpha.py"]},
                  {"description": "补充接口测试", "file_paths": ["tests/test_alpha.py"]}],
        "validation": [{"method": "运行接口测试", "expected_result": "默认值和传入值的断言均通过",
                        "command": "python -m unittest discover -s tests -v"}],
        "open_questions": ["默认参数是否继续使用 1？"],
    }


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CODEBASE_QA_LOCAL_MODE":"1"}))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.work = self.root / "work"
        self.repo = Repo.init(self.work)
        self.addCleanup(self.repo.close)
        (self.work / "alpha.py").write_text("def alpha():\n    return 1\n")
        (self.work / "other.py").write_text("OTHER = 2\n")
        (self.work / "pkg").mkdir()
        (self.work / "pkg" / "mod.py").write_text("VALUE = 1\n")
        (self.work / "linked").symlink_to("pkg", target_is_directory=True)
        self.repo.index.add(["alpha.py", "other.py", "pkg/mod.py", "linked"])
        self.actor = Actor("Fixture", "fixture@example.invalid")
        self.repo.index.commit("planning fixture", author=self.actor, committer=self.actor)
        for context in (patch.object(snapshots, "CLONE_DIR", str(self.root / "snapshots")),
                        patch.object(conversations, "CHROMA_DIR", str(self.root / "db")),
                        patch.object(models, "ALLOW_MODEL_REQUESTS", False)):
            context.start()
            self.addCleanup(context.stop)
        self.snapshot = snapshots.create_snapshot(URL, str(self.work))
        self.session = create_conversation(URL, self.snapshot.commit_sha, COLLECTION)
        self.evidence = EvidenceStore(URL, self.snapshot.root, self.snapshot.commit_sha)
        self.evidence.read("alpha.py")

    def checked(self, payload=None):
        return check_plan(ChangePlan.model_validate(payload or proposal()), self.evidence)

    def load(self):
        return load_conversation(self.session, URL, self.snapshot.commit_sha, COLLECTION)

    def test_complete_plan_has_fixed_sections_checked_sources_and_structured_data(self):
        checked = self.checked()
        self.assertTrue(checked.can_save)
        self.assertEqual(checked.plan, ChangePlan.model_validate(proposal()))
        self.assertEqual(CheckedAnswer.model_validate_json(checked.model_dump_json()), checked)
        self.assertIsNone(CheckedAnswer.model_validate({"text": "old answer"}).plan)
        for section in ("修改目标", "当前实现", "涉及文件", "修改顺序", "验证方法", "待确认"):
            self.assertIn(f"### {section}", checked.text)
        self.assertEqual(len(checked.citations), 1)
        self.assertIn(f"/blob/{self.snapshot.commit_sha}/alpha.py#L1-L2", checked.text)
        self.assertFalse((Path(self.snapshot.root) / "tests/test_alpha.py").exists())

    def test_modify_and_delete_require_exact_target_source_not_another_file(self):
        for action in ("modify", "delete"):
            data = proposal()
            data["file_changes"][0].update(file_path="other.py", action=action)
            data["steps"][0]["file_paths"] = ["other.py"]
            with self.subTest(action=action):
                checked = self.checked(data)
                self.assertFalse(checked.can_save)
                self.assertTrue(any("本文件源码依据" in issue.message for issue in checked.issues))

    def test_missing_modify_target_is_not_treated_as_a_new_file(self):
        data = proposal()
        data["file_changes"][0]["file_path"] = "absent.py"
        checked = self.checked(data)
        self.assertTrue(any("不在当前快照" in issue.message for issue in checked.issues))

    def test_new_file_requires_a_checked_integration_reference(self):
        data = proposal()
        data["file_changes"][1]["references"] = ["S99"]
        checked = self.checked(data)
        self.assertFalse(checked.can_save)
        self.assertTrue(any(issue.reference_id == "S99" for issue in checked.issues))
        self.assertTrue(any("接入位置依据" in issue.message for issue in checked.issues))

    def test_new_path_conflicts_with_files_directories_and_skipped_objects(self):
        for path in ("alpha.py", "alpha.py/child.py", "pkg", "linked", "linked/child.py"):
            with self.subTest(path=path):
                data = proposal()
                data["file_changes"][1]["file_path"] = path
                checked = self.checked(data)
                self.assertTrue(any("已有内容冲突" in issue.message for issue in checked.issues))

    def test_paths_reject_escape_drives_metadata_control_characters_and_aliases(self):
        for path in ("../alpha.py", "/tmp/alpha.py", "C:/alpha.py", "dir\\alpha.py", ".git/config",
                     "dir/../alpha.py", "./alpha.py", "dir//file.py", "dir\nfile.py", ".", "file\x00.py"):
            with self.subTest(path=path):
                data = proposal()
                data["file_changes"][1]["file_path"] = path
                self.assertTrue(any("路径无效" in issue.message for issue in self.checked(data).issues))

    def test_duplicate_targets_unknown_step_paths_and_uncovered_files_are_reported(self):
        data = proposal()
        data["file_changes"].append(deepcopy(data["file_changes"][0]))
        data["steps"] = [{"description": "未确定", "file_paths": ["unknown.py"]}]
        issues = [issue.message for issue in self.checked(data).issues]
        self.assertTrue(any("重复" in issue for issue in issues))
        self.assertTrue(any("未列入修改清单" in issue for issue in issues))
        self.assertTrue(any("未覆盖文件" in issue for issue in issues))

    def test_insufficient_information_remains_open_without_invented_changes(self):
        data = {"goal": "未知需求", "current_behavior": [], "file_changes": [], "steps": [],
                "validation": [], "open_questions": ["需要先明确哪个接口的行为？"]}
        checked = self.checked(data)
        self.assertFalse(checked.can_save)
        self.assertEqual(checked.plan.file_changes, [])
        self.assertIn("需要先明确哪个接口", checked.text)
        self.assertIn("### 规划检查", checked.text)

    def test_old_or_unknown_references_do_not_borrow_current_file_evidence(self):
        self.evidence = EvidenceStore(URL, self.snapshot.root, self.snapshot.commit_sha, reference_offset=1)
        self.evidence.read("alpha.py")  # S2; S1 belongs to an earlier question.
        checked = self.checked()
        self.assertFalse(checked.can_save)
        self.assertEqual(checked.citations, ())
        self.assertTrue(any(issue.reference_id == "S1" for issue in checked.issues))

    def test_tampered_source_and_version_mismatch_are_rejected(self):
        path = Path(self.snapshot.root) / "alpha.py"
        path.chmod(0o644)
        path.write_text("CHANGED = True\n")
        self.assertTrue(any("改变或无法核对" in issue.message for issue in self.checked().issues))
        self.evidence.commit_sha = "a" * 40
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.checked()

    def test_schema_rejects_extra_fields_empty_items_and_invalid_references(self):
        for field, value in (("goal", " "), ("file_changes", [{"action": "execute"}]),
                             ("current_behavior", [{"description": "a", "references": ["[S1]"]}]),
                             ("unknown", "x")):
            data = proposal()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ChangePlan.model_validate(data)

    def test_prose_markup_is_escaped_and_command_fences_cannot_close_early(self):
        data = proposal()
        data["goal"] = "![remote](https://example.invalid/pixel) <img src=x> [S99]"
        data["validation"][0]["command"] = "echo '```'\n[S99]\n```\nrm -rf proposed-only"
        checked = self.checked(data)
        self.assertTrue(checked.can_save)
        self.assertNotIn("![remote]", checked.text)
        self.assertNotIn("<img", checked.text)
        self.assertIn("\\[S99\\]", checked.text)
        self.assertIn("````\necho", checked.text)

    def test_proposed_commands_are_never_executed_and_source_is_unchanged(self):
        marker = self.root / "executed"
        data = proposal()
        data["validation"][0]["command"] = f"touch {marker}"
        before = (Path(self.snapshot.root) / "alpha.py").read_bytes()
        self.checked(data)
        self.assertFalse(marker.exists())
        self.assertEqual((Path(self.snapshot.root) / "alpha.py").read_bytes(), before)

    def make_agent(self, model):
        agent = Agent(FunctionModel(model), deps_type=RepoDeps)
        make_tools(agent)
        return agent

    def ask(self, question, mode="plan", **kwargs):
        return asyncio.run(qa_agent.ask(question, URL, self.snapshot.root, collection_name=COLLECTION,
                                        commit_sha=self.snapshot.commit_sha, conversation_id=self.session,
                                        mode=mode, **kwargs))

    def test_framework_uses_structured_output_and_preserves_qa_plan_qa_history(self):
        seen = []
        def model(messages, info):
            prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
            output = info.output_tools
            if prompts[-1] != "plan":
                if prompts[-1] == "follow-up":
                    self.assertEqual(prompts, ["question", "plan", "follow-up"])
                    self.assertTrue(any(isinstance(part, ToolCallPart) and part.tool_name == "submit_change_plan"
                                        for message in messages for part in message.parts))
                return ModelResponse(parts=[TextPart("local answer")])
            self.assertEqual(output[0].name, "submit_change_plan")
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                seen.append(prompts)
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py"})])
            return ModelResponse(parts=[ToolCallPart(output[0].name, proposal())])
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), \
             patch.object(qa_agent, "evaluate", return_value=0) as evaluate, patch.object(qa_agent, "save_qa") as cache:
            self.ask("question", mode="qa")
            self.assertIn("### 修改顺序", self.ask("plan"))
            self.ask("follow-up", mode="qa")
            self.assertEqual(evaluate.call_count, 2)  # Plans do not use the legacy string quality heuristic.
            cache.assert_not_called()
        state = self.load()
        self.assertEqual(state.revision, 3)
        self.assertIsNone(state.turns[0].answer.plan)
        self.assertEqual(state.turns[1].answer.plan, ChangePlan.model_validate(proposal()))
        self.assertTrue(state.turns[1].answer.can_save)
        self.assertEqual(seen, [["question", "plan"]])

    def test_invalid_structured_output_fails_once_without_saving_partial_history(self):
        calls = []
        def model(messages, info):
            calls.append(1)
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"goal": "incomplete"})])
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), patch.object(qa_agent, "save_qa") as cache:
            with self.assertRaises(UnexpectedModelBehavior):
                self.ask("plan")
            cache.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.load().revision, 0)

    def test_unpinned_or_unknown_mode_is_rejected_before_model_request(self):
        with patch.object(qa_agent, "_get_agent") as agent:
            for mode in ("plan", "unknown"):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    asyncio.run(qa_agent.ask("request", URL, self.snapshot.root, mode=mode))
            agent.assert_not_called()

    def test_stream_callback_exposes_plan_with_same_rendered_answer(self):
        def model(messages, info):
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py"})])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, proposal())])
        results = []
        async def stream():
            return "".join([part async for part in qa_agent.ask_stream(
                "plan", URL, self.snapshot.root, collection_name=COLLECTION,
                commit_sha=self.snapshot.commit_sha, conversation_id=self.session, mode="plan", on_result=results.append)])
        with patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), patch.object(qa_agent, "save_qa") as cache:
            self.assertEqual(asyncio.run(stream()), results[0].text)
            cache.assert_not_called()
        self.assertEqual(results[0].plan, self.load().turns[0].answer.plan)

    def test_streamlit_real_plan_renders_and_follow_up_retains_structured_history(self):
        # Keep the integrated UI process/lifecycle independent of later SDK tests.
        if os.environ.get("CODEBASE_QA_PLANNING_UI_CHILD") != "1":
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_planning.PlanningTests."
                 "test_streamlit_real_plan_renders_and_follow_up_retains_structured_history", "-v"],
                cwd=Path(__file__).resolve().parents[1],
                env=dict(os.environ, CODEBASE_QA_PLANNING_UI_CHILD="1"),
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            return
        def model(messages, info):
            if not info.output_tools:
                prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
                self.assertEqual(prompts, ["生成修改方案", "解释验证步骤"])
                return ModelResponse(parts=[TextPart("本地测试样例：补充验证说明")])
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py"})])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, proposal())])
        repository = RepositoryIndex(URL, self.snapshot.commit_sha, self.snapshot.root, COLLECTION, 3, 3, "2026-11-11", "fixture")
        with patch("indexer.repository_service.get_current_repository", return_value=repository), \
             patch("indexer.repository_service.list_repository_versions", return_value=[repository]), \
             patch("indexer.repository_service.get_repository_version", return_value=repository), \
             patch("memory.knowledge.get_memory_stats", return_value={"count": 0, "latest": None}), \
             patch.object(qa_agent, "_get_agent", return_value=self.make_agent(model)), patch.object(qa_agent, "save_qa"):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=60)
            app.text_input[0].set_value(URL).run(timeout=60)
            app.radio[0].set_value("修改规划").run(timeout=60)
            app.chat_input[0].set_value("生成修改方案").run(timeout=60)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.error), 0)
            session = app.session_state["conversation_id"]
            self.assertEqual(app.session_state["messages"][-1]["sources"]["plan"]["goal"], proposal()["goal"])
            self.assertIn("修改顺序", app.session_state["messages"][-1]["content"])
            app.run(timeout=60)
            self.assertEqual(app.session_state["conversation_id"], session)
            app.radio[0].set_value("问答").run(timeout=60)
            app.chat_input[0].set_value("解释验证步骤").run(timeout=60)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.error), 0)
            state = load_conversation(session, URL, self.snapshot.commit_sha, COLLECTION)
            self.assertEqual(state.revision, 2)
            self.assertEqual(state.turns[0].answer.plan, ChangePlan.model_validate(proposal()))
            self.assertIsNone(state.turns[1].answer.plan)


if __name__ == "__main__":
    unittest.main()
