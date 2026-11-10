"""Exercise source checking, a local FunctionModel tool loop and real MCP stdio."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic_ai import Agent, models
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent import qa_agent
from agent.evidence import CITATION_INSTRUCTIONS, EvidenceStore
from agent.tools import RepoDeps, make_tools
from indexer.citations import check_source_citation
from indexer.repository_service import get_current_repository

URL = "https://github.com/xyma2003/codebase-qa"


async def check_framework(repository):
    calls = []
    def model(messages, info):
        assert CITATION_INSTRUCTIONS in info.instructions
        calls.append(len(messages))
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "config.py", "start_line": 10, "end_line": 15}, "local-fixture")])
        return ModelResponse(parts=[TextPart("本地测试样例：默认模型在 config.py 第 10 行配置 [S1]。")])
    agent = Agent(FunctionModel(model), deps_type=RepoDeps)
    make_tools(agent)
    checked = []
    with patch.object(qa_agent, "_get_agent", return_value=agent), patch.object(qa_agent, "save_qa") as save:
        answer = "".join([part async for part in qa_agent.ask_stream(
            "本地测试：默认模型在哪里配置？", URL, repository.repo_root,
            commit_sha=repository.commit_sha, collection_name=repository.collection_name,
            on_result=checked.append,
        )])
    assert len(calls) == 2 and checked[0].can_save
    assert checked[0].citations[0].commit_sha == repository.commit_sha
    assert answer == checked[0].text
    store = EvidenceStore(URL, repository.repo_root, repository.commit_sha)
    store.read("config.py", 10, 15)
    invalid = store.check_answer("本地测试：伪造编号 [S99]")
    assert not invalid.can_save and not invalid.citations
    return {"model": "local FunctionModel fixture", "external_model_called": False,
            "model_turns": len(calls), "answer": checked[0].model_dump(mode="json"),
            "unknown_identifier_rejected": True, "memory_write_mocked": True,
            "memory_save_calls": save.call_count}


async def check_mcp(repository):
    results = {}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "verify_source_citation" in {tool.name for tool in tools.tools}

            async def call(name, arguments, label):
                result = await session.call_tool(name, arguments)
                text = "\n".join(getattr(item, "text", "") for item in result.content)
                results[label] = {"tool": name, "arguments": arguments, "is_error": bool(result.isError), "text": text}
                return result, text

            _, text = await call("set_repo", {"repo_url": URL}, "select_repository")
            assert repository.commit_sha in text
            _, text = await call("read_file", {"file_path": "config.py", "start_line": 10, "end_line": 10}, "read_source")
            assert "10 | OPENAI_MODEL" in text
            source = check_source_citation(repository.repo_root, URL, repository.commit_sha,
                                           "config.py", 10, 10, 'OPENAI_MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen3-32B")')
            arguments = dict(file_path=source.file_path, start_line=10, end_line=10,
                             source_text=source.source_text, commit_sha=repository.commit_sha)
            result, text = await call("verify_source_citation", arguments, "valid_quote")
            data = result.structuredContent or json.loads(text)
            assert data["valid"] and data["url"] == source.url
            for label, changes in (
                ("wrong_quote", {"source_text": "FORGED_QUOTE"}),
                ("wrong_version", {"commit_sha": "0" * 40}),
                ("out_of_range", {"end_line": 99999}),
                ("path_escape", {"file_path": "../config.py"}),
            ):
                result, text = await call("verify_source_citation", arguments | changes, label)
                assert not result.isError
                assert not (result.structuredContent or json.loads(text))["valid"]
            result, _ = await call("verify_source_citation", arguments | {"start_line": True}, "boolean_line_rejected")
            assert result.isError
            await call("set_repo", {"repo_url": "file:///invalid"}, "invalid_selection")
            result, text = await call("verify_source_citation", arguments, "no_stale_repository")
            assert not (result.structuredContent or json.loads(text))["valid"]
            return {"tool_count": len(tools.tools), "calls_passed": len(results), "results": results}


async def main():
    repository = get_current_repository(URL)
    assert repository is not None, "Prepare the recorded repository index first."
    # Block network model requests even if local credentials are later configured.
    models.ALLOW_MODEL_REQUESTS = False
    return {"status": "passed", "chat_model_called": False, "repo_url": URL,
            "commit_sha": repository.commit_sha, "instructions": CITATION_INSTRUCTIONS,
            "framework": await check_framework(repository), "mcp": await check_mcp(repository)}


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/checks/20261110-citations.json"))
    destination = parser.parse_args().output.resolve()
    if destination.exists():
        raise RuntimeError("Choose a new --output; saved evidence is not overwritten.")
    report = asyncio.run(asyncio.wait_for(main(), 120))
    paths = ("agent/evidence.py", "indexer/citations.py", "agent/tools.py", "agent/qa_agent.py", "app.py", "mcp_server.py", "config.py", "scripts/check_citations.py")
    report["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "chat_model_called": False,
                      "framework_turns": report["framework"]["model_turns"], "mcp_calls": report["mcp"]["calls_passed"]}))
