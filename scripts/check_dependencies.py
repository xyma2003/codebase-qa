"""Real snapshot/MCP checks plus a local dependencies→read→cite fixture."""

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
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agent import qa_agent
from agent.tools import RepoDeps, make_tools
from indexer import vector_store
from indexer.citations import check_source_citation
from indexer.dependencies import dependency_payload, get_file_dependencies
from indexer.repository_service import get_current_repository

URL = "https://github.com/xyma2003/codebase-qa"
SOURCE = "agent/qa_agent.py"
TARGET = "indexer/vector_store.py"


async def framework(repository):
    turns, results = [], []
    def model(messages, info):
        turns.append(len(messages))
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart("get_file_dependencies", {"file_path": SOURCE}, "fixture-deps")])
        if len(turns) == 2:
            returned = next(part for message in reversed(messages) for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "get_file_dependencies")
            reference = next(row for row in json.loads(returned.content)["references"] if any(target["file_path"] == "agent/tools.py" for target in row["targets"]))
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": reference["source_file"], "start_line": reference["start_line"], "end_line": reference["end_line"]}, "fixture-read")])
        return ModelResponse(parts=[TextPart("本地测试样例：agent/qa_agent.py 的导入指向 agent/tools.py 候选 [S1]。")])
    agent = Agent(FunctionModel(model), deps_type=RepoDeps)
    make_tools(agent)
    with patch.object(qa_agent, "_get_agent", return_value=agent), patch.object(qa_agent, "save_qa"):
        answer = "".join([part async for part in qa_agent.ask_stream(
            "本地测试：跨文件导入线索", URL, repository.repo_root,
            collection_name=repository.collection_name, commit_sha=repository.commit_sha, on_result=results.append,
        )])
    assert len(turns) == 3 and results[0].can_save and answer == results[0].text
    return {"model": "local FunctionModel fixture", "model_turns": len(turns), "memory_write_mocked": True,
            "answer": results[0].model_dump(mode="json")}


async def protocol(repository, outgoing, incoming):
    calls = {}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "get_file_dependencies" in {tool.name for tool in tools.tools}
            async def call(name, arguments, label, error=False):
                response = await session.call_tool(name, arguments)
                assert bool(response.isError) == error, label
                text = "\n".join(getattr(item, "text", "") for item in response.content)
                calls[label] = {"tool": name, "arguments": arguments, "is_error": bool(response.isError), "text": text}
                return json.loads(text) if not error and name in ("get_file_dependencies", "verify_source_citation") else text
            assert repository.commit_sha in await call("set_repo", {"repo_url": URL}, "select")
            assert await call("get_file_dependencies", {"file_path": SOURCE}, "outgoing") == dependency_payload(outgoing)
            assert await call("get_file_dependencies", {"file_path": TARGET, "direction": "incoming"}, "incoming") == dependency_payload(incoming)
            source = next(row.evidence for row in outgoing.references if row.evidence and row.targets)
            args = source.model_dump(exclude={"repo_url", "file_sha256"})
            assert (await call("verify_source_citation", args, "verify_source"))["valid"]
            first = await call("get_file_dependencies", {"file_path": SOURCE, "limit": 1}, "first_page")
            assert first["next_offset"] == 1
            second = await call("get_file_dependencies", {"file_path": SOURCE, "limit": 1, "offset": first["next_offset"]}, "second_page")
            assert first["references"] != second["references"]
            assert "error" in await call("get_file_dependencies", {"file_path": SOURCE, "source_root": "../"}, "root_escape")
            await call("get_file_dependencies", {"file_path": SOURCE, "direction": "both"}, "invalid_direction", error=True)
            await call("get_file_dependencies", {"file_path": SOURCE, "limit": True}, "boolean_limit", error=True)
            await call("set_repo", {"repo_url": "file:///invalid"}, "invalid_selection")
            assert "error" in await call("get_file_dependencies", {"file_path": SOURCE}, "cleared_context")
            return {"tool_count": len(tools.tools), "calls_passed": len(calls), "results": calls}


async def main():
    models.ALLOW_MODEL_REQUESTS = False
    repository = get_current_repository(URL)
    assert repository is not None
    with patch.object(vector_store, "_get_client", side_effect=AssertionError("Dependency lookup must read source")), patch.object(vector_store, "_get_embed_fn", side_effect=AssertionError("No embedding needed")):
        outgoing = get_file_dependencies(repository.repo_root, URL, repository.commit_sha, SOURCE)
        incoming = get_file_dependencies(repository.repo_root, URL, repository.commit_sha, TARGET, "incoming")
        assert {"agent/tools.py", "config.py"}.issubset({target.file_path for row in outgoing.references for target in row.targets})
        assert {"agent/tools.py", "mcp_server.py", "indexer/__init__.py"}.issubset({row.source_file for row in incoming.references})
        assert not outgoing.scan_limited and not incoming.scan_limited
        for row in outgoing.references + incoming.references:
            if row.evidence:
                assert check_source_citation(repository.repo_root, **row.evidence.model_dump()) == row.evidence
    report = {"status": "passed", "chat_model_called": False, "commit_sha": repository.commit_sha,
              "outgoing": dependency_payload(outgoing), "incoming": dependency_payload(incoming),
              "framework": await framework(repository), "mcp": await protocol(repository, outgoing, incoming)}
    assert get_current_repository(URL) == repository
    report["active_index_unchanged"] = True
    return report


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/checks/20261110-dependencies.json"))
    destination = parser.parse_args().output.resolve()
    if destination.exists():
        raise RuntimeError("Choose a new --output; saved evidence is not overwritten.")
    report = asyncio.run(asyncio.wait_for(main(), 120))
    paths = ("indexer/dependencies.py", "agent/tools.py", "agent/qa_agent.py", "mcp_server.py", "scripts/check_dependencies.py", "config.py", "indexer/source_reader.py")
    report["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "outgoing_rows": len(report["outgoing"]["references"]), "incoming_rows": len(report["incoming"]["references"]),
                      "framework_turns": report["framework"]["model_turns"], "mcp_calls": report["mcp"]["calls_passed"], "chat_model_called": False}))
