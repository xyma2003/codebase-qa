"""Exercise find/read/structured planning and a follow-up on the saved source version."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic_ai import Agent, models
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from agent import conversations, qa_agent
from agent.conversations import create_conversation, load_conversation
from agent.planning import ChangePlan
from agent.tools import RepoDeps, make_tools
from indexer import vector_store
from indexer.citations import check_source_citation
from indexer.repository_service import get_current_repository

URL = "https://github.com/xyma2003/codebase-qa"
QUESTION = "本地测试：为选定的旧版本规划真正的多轮对话。"
FOLLOW_UP = "本地测试：继续解释页面接入的位置。"


def fixture(references):
    qa, app = references["agent/qa_agent.py"], references["app.py"]
    return ChangePlan.model_validate({
        "goal": "本地测试样例：为选定旧版本添加原生多轮对话",
        "current_behavior": [
            {"description": "旧版本 ask_stream 只向 Agent 传递当前问题与仓库依赖", "references": [qa]},
            {"description": "旧版本页面保存显示消息，但调用问答入口时没有传会话", "references": [app]},
        ],
        "file_changes": [
            {"file_path": "memory/conversations.py", "action": "create", "reason": "为原生消息增加会话存储",
             "changes": ["以仓库、版本和会话区分历史", "完整轮次原子保存"], "references": [qa]},
            {"file_path": "agent/qa_agent.py", "action": "modify", "reason": "让模型收到已保存的原生历史",
             "changes": ["读取同版本会话", "传入 message_history", "只追加新轮次消息"], "references": [qa]},
            {"file_path": "app.py", "action": "modify", "reason": "保持当前会话并传给问答入口",
             "changes": ["在页面会话中保持编号", "切换仓库或版本时开始独立对话"], "references": [app]},
            {"file_path": "tests/test_conversations.py", "action": "create", "reason": "验证上下文延续与版本隔离",
             "changes": ["用本地模型检查第二问收到第一问", "验证失败保留与新会话隔离"], "references": [qa, app]},
        ],
        "steps": [
            {"description": "增加会话存储接口", "file_paths": ["memory/conversations.py"]},
            {"description": "接入原生历史与轮次保存", "file_paths": ["agent/qa_agent.py"]},
            {"description": "页面传递会话编号并处理版本切换", "file_paths": ["app.py"]},
            {"description": "补充跨轮次与失败测试", "file_paths": ["tests/test_conversations.py"]},
        ],
        "validation": [
            {"method": "完成同版本的两轮问答", "expected_result": "第二轮收到第一轮原生问题、回答和工具结果",
             "command": None},
            {"method": "运行会话测试", "expected_result": "会话和版本隔离、失败保留等断言通过",
             "command": "python -m unittest discover -s tests -v"},
        ],
        "open_questions": ["历史会话选择与长对话压缩是否作为后续独立功能？"],
    })


async def main():
    models.ALLOW_MODEL_REQUESTS = False
    repository = get_current_repository(URL)
    assert repository is not None
    observed, answers = [], []

    async def model(messages, info):
        prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        # Only inspect this run's results when choosing its next tool/citation.
        last_request = max(index for index, message in enumerate(messages)
                           if any(isinstance(part, UserPromptPart) for part in message.parts))
        # The SDK may merge the previous output tool's acknowledgement with the
        # next user request. It is history, not a new read/search result.
        returned = [part for message in messages[last_request:] for part in message.parts
                    if isinstance(part, ToolReturnPart) and part.tool_name in {"read_file", "find_code"}]
        observed.append({"questions": prompts, "output_tools": [tool.name for tool in info.output_tools],
                         "current_tool_results": [part.tool_name for part in returned]})
        if prompts[-1] == FOLLOW_UP:
            assert prompts == [QUESTION, FOLLOW_UP] and not info.output_tools
            assert any(isinstance(part, ToolCallPart) and part.tool_name == "submit_change_plan"
                       for message in messages for part in message.parts)
            if not returned:
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "app.py", "start_line": 95, "end_line": 113})])
            reference = re.search(r"\[(S\d+)\]", str(returned[-1].content))[1]
            return ModelResponse(parts=[TextPart(f"本地测试样例：旧版本页面在 app.py 调用 ask_stream，应在此接入会话 [{reference}]。")])
        assert prompts == [QUESTION] and info.output_tools[0].name == "submit_change_plan"
        if not returned:
            return ModelResponse(parts=[ToolCallPart("find_code", {"query": "ask_stream", "mode": "symbol"})])
        if returned[-1].tool_name == "find_code":
            assert any(hit["file_path"] == "agent/qa_agent.py" for hit in json.loads(returned[-1].content)["hits"])
            return ModelResponse(parts=[
                ToolCallPart("read_file", {"file_path": "agent/qa_agent.py", "start_line": 66, "end_line": 82}, "read-qa"),
                ToolCallPart("read_file", {"file_path": "app.py", "start_line": 95, "end_line": 113}, "read-app"),
            ])
        references = {}
        for part in returned:
            if part.tool_name == "read_file":
                content = str(part.content)
                path = "agent/qa_agent.py" if "**agent/qa_agent.py**" in content else "app.py"
                references[path] = re.search(r"\[(S\d+)\]", content)[1]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, fixture(references).model_dump(mode="json"))])

    agent = Agent(FunctionModel(model), deps_type=RepoDeps)
    make_tools(agent)
    with tempfile.TemporaryDirectory(prefix="codebase-qa-planning-") as directory, \
         patch.object(conversations, "CHROMA_DIR", directory), patch.object(qa_agent, "_get_agent", return_value=agent), \
         patch.object(qa_agent, "save_qa") as cache, \
         patch.object(vector_store, "_get_client", side_effect=AssertionError("No vector access expected")), \
         patch.object(vector_store, "_get_embed_fn", side_effect=AssertionError("No embedding expected")):
        session = create_conversation(URL, repository.commit_sha, repository.collection_name)
        for question, mode in ((QUESTION, "plan"), (FOLLOW_UP, "qa")):
            text = "".join([part async for part in qa_agent.ask_stream(
                question, URL, repository.repo_root, collection_name=repository.collection_name,
                commit_sha=repository.commit_sha, conversation_id=session, mode=mode, on_result=answers.append)])
            assert text == answers[-1].text and answers[-1].can_save
        state = load_conversation(session, URL, repository.commit_sha, repository.collection_name)
        assert state.revision == 2 and state.turns[0].answer.plan == answers[0].plan and state.turns[1].answer.plan is None
        assert len(answers[0].plan.file_changes) == 4
        assert len(observed) == 5 and state.reference_count == 3
        cache.assert_not_called()
        for answer in answers:
            for source in answer.citations:
                check_source_citation(repository.repo_root, **source.model_dump(exclude={"reference_id"}))
        code = (
            "import json; from agent import conversations as c; "
            f"c.CHROMA_DIR={directory!r}; "
            f"s=c.load_conversation({session!r},{URL!r},{repository.commit_sha!r},{repository.collection_name!r}); "
            "print(json.dumps({'revision':s.revision,'plan_files':[f.file_path for f in s.turns[0].answer.plan.file_changes],"
            "'follow_up_is_qa':s.turns[1].answer.plan is None}))"
        )
        child = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30)
        readback = json.loads(child.stdout)
        assert readback == {"revision": 2, "plan_files": [change.file_path for change in answers[0].plan.file_changes], "follow_up_is_qa": True}
    assert get_current_repository(URL) == repository
    return {"status": "passed", "chat_model_called": False, "model": "local FunctionModel fixture",
            "commit_sha": repository.commit_sha, "collection_name": repository.collection_name,
            "framework_model_steps": len(observed), "observed_history": observed,
            "answers": [answer.model_dump(mode="json") for answer in answers], "separate_process_readback": readback,
            "stored_native_message_count": len(state.messages), "stored_conversation_revision": state.revision,
            "standalone_cache_mocked": True, "cache_writes": 0, "active_index_unchanged": True,
            "target_files_written": False, "validation_commands_executed": False,
            "database": "temporary SQLite database, removed after verification"}


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/checks/20261111-planning.json"))
    destination = parser.parse_args().output.resolve()
    if destination.exists():
        raise RuntimeError("Choose a new --output; saved evidence is not overwritten.")
    report = asyncio.run(asyncio.wait_for(main(), 120))
    paths = ("agent/planning.py", "agent/evidence.py", "agent/qa_agent.py", "app.py", "scripts/check_planning.py", "config.py")
    report["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "framework_model_steps": report["framework_model_steps"],
                      "planned_files": len(report["answers"][0]["plan"]["file_changes"]), "chat_model_called": False}))
