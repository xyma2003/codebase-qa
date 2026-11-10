"""Check real snapshot facts, MCP and a local overview→read→cite fixture."""

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
from indexer.overview import build_repository_overview, overview_payload
from indexer.repository_service import get_current_repository

URL = "https://github.com/xyma2003/codebase-qa"


async def framework(repository):
    turns, results = [], []
    def model(messages, info):
        turns.append(len(messages))
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart("get_repo_overview", {}, "fixture-overview")])
        if len(turns) == 2:
            returned = next(part for message in reversed(messages) for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "get_repo_overview")
            source = next(item["evidence"] for item in json.loads(returned.content)["entry_clues"] if item["kind"] == "main_guard")
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": source["file_path"], "start_line": source["start_line"], "end_line": source["end_line"]}, "fixture-read")])
        return ModelResponse(parts=[TextPart("本地测试样例：mcp_server.py 包含 Python 启动判断 [S1]。")])
    agent = Agent(FunctionModel(model), deps_type=RepoDeps)
    make_tools(agent)
    with patch.object(qa_agent, "_get_agent", return_value=agent), patch.object(qa_agent, "save_qa"):
        answer = "".join([part async for part in qa_agent.ask_stream(
            "本地测试：查看项目入口线索", URL, repository.repo_root,
            collection_name=repository.collection_name, commit_sha=repository.commit_sha, on_result=results.append,
        )])
    assert len(turns) == 3 and results[0].can_save
    assert results[0].citations[0].file_path == "mcp_server.py"
    assert answer == results[0].text
    return {"model": "local FunctionModel fixture", "model_turns": len(turns), "memory_write_mocked": True,
            "answer": results[0].model_dump(mode="json")}


async def protocol(repository, overview):
    calls = {}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "get_repo_overview" in {tool.name for tool in tools.tools}
            async def call(name, arguments, label):
                response = await session.call_tool(name, arguments)
                assert not response.isError, label
                text = "\n".join(getattr(item, "text", "") for item in response.content)
                calls[label] = {"tool": name, "arguments": arguments, "text": text}
                return json.loads(text) if name in ("get_repo_overview", "verify_source_citation") else text
            assert repository.commit_sha in await call("set_repo", {"repo_url": URL}, "select")
            data = await call("get_repo_overview", {}, "overview")
            assert data == overview_payload(overview)
            source = next(item.evidence for item in overview.entry_clues if item.kind == "main_guard")
            arguments = source.model_dump(exclude={"repo_url", "file_sha256"})
            assert (await call("verify_source_citation", arguments, "verify_entry_source"))["valid"]
            await call("set_repo", {"repo_url": URL, "commit_sha": repository.commit_sha}, "explicit_version")
            assert (await call("get_repo_overview", {}, "same_version")) == data
            await call("set_repo", {"repo_url": "file:///invalid"}, "invalid_selection")
            assert "error" in await call("get_repo_overview", {}, "cleared_context")
            return {"tool_count": len(tools.tools), "calls_passed": len(calls), "results": calls}


async def main():
    models.ALLOW_MODEL_REQUESTS = False
    repository = get_current_repository(URL)
    assert repository is not None
    with patch.object(vector_store, "_get_client", side_effect=AssertionError("Overview must read source")), patch.object(vector_store, "_get_embed_fn", side_effect=AssertionError("No embeddings needed")):
        overview = build_repository_overview(repository.repo_root, URL, repository.commit_sha)
        assert overview.readme and overview.file_count == 20 and overview.python_file_count == 13
        assert {item.kind for item in overview.entry_clues} == {"main_guard", "streamlit_page"}
        assert not overview.scan_limited and overview.skipped_count == 0
        for source in [overview.readme] + [item.evidence for item in overview.entry_clues]:
            assert check_source_citation(repository.repo_root, **source.model_dump()) == source
    report = {"status": "passed", "chat_model_called": False, "commit_sha": repository.commit_sha,
              "overview": overview_payload(overview), "framework": await framework(repository),
              "mcp": await protocol(repository, overview)}
    assert get_current_repository(URL) == repository
    report["active_index_unchanged"] = True
    return report


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/checks/20261110-overview.json"))
    destination = parser.parse_args().output.resolve()
    if destination.exists():
        raise RuntimeError("Choose a new --output; saved evidence is not overwritten.")
    report = asyncio.run(asyncio.wait_for(main(), 120))
    paths = ("indexer/overview.py", "app.py", "agent/tools.py", "agent/qa_agent.py", "mcp_server.py", "scripts/check_overview.py", "config.py", "indexer/source_reader.py")
    report["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "files": report["overview"]["file_count"], "python_files": report["overview"]["python_file_count"],
                      "framework_turns": report["framework"]["model_turns"], "mcp_calls": report["mcp"]["calls_passed"], "chat_model_called": False}))
