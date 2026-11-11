"""Check native multi-turn context against the existing snapshot, without a chat API."""

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
from agent.tools import RepoDeps, make_tools
from indexer import vector_store
from indexer.citations import check_source_citation
from indexer.repository_service import get_current_repository

URL = "https://github.com/xyma2003/codebase-qa"
FIRST = "本地测试：qa_agent.py 调用了什么工具模块？"
FOLLOW_UP = "本地测试：继续看刚才提到的那个工具模块。"
STALE = "本地测试：直接复制上一轮引用。"
FRESH = "本地测试：新建对话。"


async def main():
    models.ALLOW_MODEL_REQUESTS = False
    repository = get_current_repository(URL)
    assert repository is not None
    observed, answers = [], []

    def model(messages, info):
        prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        observed.append({"questions": prompts, "native_message_count": len(messages)})
        if prompts[-1] == FOLLOW_UP:
            assert prompts == [FIRST, FOLLOW_UP]
            assert any(isinstance(part, ToolReturnPart) and part.tool_name == "read_file" and "[S1]" in str(part.content)
                       for message in messages for part in message.parts)
            assert any(isinstance(part, TextPart) and "agent/tools.py" in part.content
                       for message in messages for part in message.parts)
        if prompts[-1] == STALE:
            assert prompts == [FIRST, FOLLOW_UP, STALE]
            return ModelResponse(parts=[TextPart("本地测试样例：复制旧引用 [S1]。")])
        if prompts[-1] == FRESH:
            assert prompts == [FRESH]
        if not isinstance(messages[-1].parts[-1], ToolReturnPart):
            path = "agent/tools.py" if prompts[-1] == FOLLOW_UP else "agent/qa_agent.py"
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": path, "start_line": 1, "end_line": 12})])
        returned = messages[-1].parts[-1]
        reference = re.search(r"\[(S\d+)\]", str(returned.content))[1]
        return ModelResponse(parts=[TextPart(f"本地测试样例：qa_agent 使用 agent/tools.py 中的工具 [{reference}]。")])

    def new_agent():
        agent = Agent(FunctionModel(model), deps_type=RepoDeps)
        make_tools(agent)
        return agent

    async def ask(question, session):
        text = "".join([chunk async for chunk in qa_agent.ask_stream(
            question, URL, repository.repo_root, collection_name=repository.collection_name,
            commit_sha=repository.commit_sha, conversation_id=session, on_result=answers.append)])
        assert text == answers[-1].text

    with tempfile.TemporaryDirectory(prefix="codebase-qa-conversations-") as directory, \
         patch.object(conversations, "CHROMA_DIR", directory), \
         patch.object(vector_store, "_get_client", side_effect=AssertionError("No vector writes expected")), \
         patch.object(vector_store, "_get_embed_fn", side_effect=AssertionError("No embedding expected")), \
         patch.object(qa_agent, "evaluate", return_value=1), patch.object(qa_agent, "save_qa") as cache:
        session = create_conversation(URL, repository.commit_sha, repository.collection_name)
        with patch.object(qa_agent, "_get_agent", return_value=new_agent()):
            await ask(FIRST, session)
        # Read in another Python process, not through the parent's module/session state.
        code = (
            "import json; from agent import conversations as c; "
            f"c.CHROMA_DIR={directory!r}; "
            f"s=c.load_conversation({session!r}, {URL!r}, {repository.commit_sha!r}, {repository.collection_name!r}); "
            "print(json.dumps({'revision':s.revision,'native_message_count':len(s.messages),"
            "'reference_count':s.reference_count,'questions':[t.question for t in s.turns]}))"
        )
        child = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30)
        readback = json.loads(child.stdout)
        assert readback == {"revision": 1, "native_message_count": 4, "reference_count": 1, "questions": [FIRST]}
        with patch.object(qa_agent, "_get_agent", return_value=new_agent()):
            await ask(FOLLOW_UP, session)
            await ask(STALE, session)
        assert answers[0].citations[0].reference_id == "S1"
        assert answers[1].citations[0].reference_id == "S2"
        assert answers[2].issues[0].reference_id == "S1" and not answers[2].citations
        state = load_conversation(session, URL, repository.commit_sha, repository.collection_name)
        assert state.revision == 3 and len(state.messages) == 10 and state.reference_count == 2
        for turn in state.turns:
            for source in turn.answer.citations:
                check_source_citation(repository.repo_root, **source.model_dump(exclude={"reference_id"}))
        with patch.object(qa_agent, "_get_agent") as forbidden:
            try:
                await qa_agent.ask("wrong version", URL, repository.repo_root, commit_sha="0" * 40,
                                   collection_name=repository.collection_name, conversation_id=session)
            except ValueError as exc:
                mismatch = str(exc)
            else:
                raise AssertionError("Version mismatch was not rejected")
            forbidden.assert_not_called()
        fresh_session = create_conversation(URL, repository.commit_sha, repository.collection_name)
        with patch.object(qa_agent, "_get_agent", return_value=new_agent()):
            await ask(FRESH, fresh_session)
        assert answers[3].citations[0].reference_id == "S1"
        assert cache.call_count == 2  # Only the initial question in each conversation.
        report = {
            "status": "passed", "chat_model_called": False, "model": "local FunctionModel fixture",
            "commit_sha": repository.commit_sha, "collection_name": repository.collection_name,
            "completed_questions": 4, "framework_model_steps": len(observed), "observed_history": observed,
            "separate_process_readback": readback, "version_mismatch_error": mismatch,
            "answers": [answer.model_dump(mode="json") for answer in answers],
            "stored_conversation_revision": state.revision, "stored_native_message_count": len(state.messages),
            "reference_count": state.reference_count, "standalone_cache_mocked": True,
            "standalone_cache_writes": cache.call_count, "follow_up_cache_writes": 0,
            "new_conversation_isolated": fresh_session != session,
            "database": "temporary SQLite database, removed after verification",
        }
    assert get_current_repository(URL) == repository
    report["active_index_unchanged"] = True
    return report


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/checks/20261111-conversations.json"))
    destination = parser.parse_args().output.resolve()
    if destination.exists():
        raise RuntimeError("Choose a new --output; saved evidence is not overwritten.")
    report = asyncio.run(asyncio.wait_for(main(), 120))
    paths = ("agent/conversations.py", "agent/evidence.py", "agent/qa_agent.py", "app.py", "scripts/check_conversations.py", "config.py")
    report["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "completed_questions", "framework_model_steps", "stored_native_message_count", "chat_model_called")}))
