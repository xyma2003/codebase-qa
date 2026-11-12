"""Save a clearly labelled local demo for browser history restoration checks."""

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from agent import qa_agent
from agent.conversations import create_conversation, load_conversation, list_conversations, display_messages
from agent.tools import RepoDeps
from evaluation.dataset import digest
from indexer.repository_service import get_current_repository


async def check():
    repository = get_current_repository("https://github.com/xyma2003/codebase-qa")
    if repository is None:
        raise ValueError("Use an already indexed repository; this check does not build or update one")
    session = create_conversation(repository.repo_url, repository.commit_sha, repository.collection_name)
    async def respond(messages, info):
        latest = max(number for number, message in enumerate(messages) if any(isinstance(part, UserPromptPart) for part in message.parts))
        returns = [part for message in messages[latest:] for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "config.py", "start_line": 1, "end_line": 12})])
        reference = re.search(r"\[(S\d+)\]", returns[-1].content)[1]
        return ModelResponse(parts=[TextPart(f"本地模拟：工具已读取所选版本的 config.py。此回答只用于验证历史恢复、引用与工具展示 [{reference}]。")])
    agent = qa_agent.build_agent(FunctionModel(respond, model_name="local-history-ui-fixture"))
    events = []
    original = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    try:
        for question in ("本地模拟：检查保存的对话和工具记录", "本地模拟：继续读取同一版本，验证追问恢复"):
            deps = RepoDeps(repository.repo_url, repository.repo_root, repository.collection_name, repository.commit_sha,
                            source_only=True, use_answer_cache=False)
            await qa_agent._run_answer(question, deps, session, agent=agent, on_event=events.append)
    finally:
        models.ALLOW_MODEL_REQUESTS = original
    history = load_conversation(session, repository.repo_url, repository.commit_sha, repository.collection_name)
    assert history.revision == 2 and len(display_messages(history)) == 4
    assert all(len(turn.answer.tool_events) == 1 for turn in history.turns)
    assert len(events) == 4 and len({event.call_id for event in events}) == 2
    return {"status": "history_tools_checks_passed", "conversation_id": session, "repo_url": repository.repo_url,
            "commit_sha": repository.commit_sha, "collection_name": repository.collection_name,
            "completed_turns": 2, "tool_calls": 2, "real_model_requests": 0,
            "demo_storage": "app_conversation_store_explicit_local_fixture", "real_model_quality_evaluated": False,
            "implementation_sha256": {name: digest((ROOT / name).read_bytes()) for name in
                ("app.py", "agent/conversations.py", "agent/tools.py", "agent/tracing.py", "agent/evidence.py", "agent/qa_agent.py")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs/checks/20261112-history-tools.json")
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("Choose a new output path")
    result = asyncio.run(check())
    with args.output.open("x", encoding="utf-8") as file:
        file.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("status", "conversation_id", "real_model_requests")}))
