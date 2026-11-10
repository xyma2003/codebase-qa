"""Native snapshot/MCP checks and a local find→read→cite FunctionModel fixture."""

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
from indexer.code_search import MAX_FILE_BYTES, MAX_SCAN_BYTES, MAX_SCAN_FILES, find_in_snapshot
from indexer.repository_service import get_current_repository

URL = "https://github.com/xyma2003/codebase-qa"


def check_native(repository):
    outputs = {}
    # Exact lookup must not use Chroma or instantiate an embedding function.
    with patch.object(vector_store, "_get_client", side_effect=AssertionError("Lookup must read source")), patch.object(vector_store, "_get_embed_fn", side_effect=AssertionError("No embeddings needed")):
        for label, kwargs in (
            ("path", dict(query="CONFIG.PY", mode="path")),
            ("python_definition", dict(query="_get_agent", mode="symbol")),
            ("module_configuration", dict(query='os.getenv("OPENAI_MODEL"', path_filter="config.py")),
            ("module_import", dict(query="from dotenv import", path_filter="config.py")),
            ("no_match", dict(query="__ABSENT_LOCAL_FIXTURE_LITERAL__")),
        ):
            result = find_in_snapshot(repository.repo_root, URL, repository.commit_sha, **kwargs)
            outputs[label] = {"arguments": kwargs, "result": result.model_dump(mode="json")}
        assert outputs["path"]["result"]["hits"][0]["file_path"] == "config.py"
        assert outputs["python_definition"]["result"]["hits"][0]["file_path"] == "agent/qa_agent.py"
        assert outputs["module_configuration"]["result"]["hits"][0]["start_line"] == 10
        assert outputs["module_import"]["result"]["hits"][0]["start_line"] == 2
        assert not outputs["no_match"]["result"]["hits"]
        assert not outputs["no_match"]["result"]["scan_limited"]
    return outputs


async def check_framework(repository):
    turns = []
    def model(messages, info):
        turns.append(len(messages))
        assert "find_code" in {tool.name for tool in info.function_tools}
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart("find_code", {"query": 'os.getenv("OPENAI_MODEL"', "path_filter": "config.py"}, "fixture-find")])
        if len(turns) == 2:
            returned = next(part for message in reversed(messages) for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "find_code")
            hit = json.loads(returned.content)["hits"][0]
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": hit["file_path"], "start_line": hit["start_line"], "end_line": hit["end_line"]}, "fixture-read")])
        return ModelResponse(parts=[TextPart("本地测试样例：config.py 第 10 行配置默认模型 Qwen/Qwen3-32B [S1]。")])
    agent = Agent(FunctionModel(model), deps_type=RepoDeps)
    make_tools(agent)
    results = []
    with patch.object(qa_agent, "_get_agent", return_value=agent), patch.object(qa_agent, "save_qa"):
        answer = "".join([part async for part in qa_agent.ask_stream(
            "本地测试：默认模型在哪里？", URL, repository.repo_root,
            collection_name=repository.collection_name, commit_sha=repository.commit_sha, on_result=results.append,
        )])
    assert len(turns) == 3 and results[0].can_save
    assert f"/blob/{repository.commit_sha}/config.py#L10-L10" in answer
    return {"model": "local FunctionModel fixture", "model_turns": len(turns),
            "chat_model_called": False, "memory_write_mocked": True,
            "answer": results[0].model_dump(mode="json")}


async def check_mcp(repository):
    results = {}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], cwd=str(ROOT), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "find_code" in {tool.name for tool in tools.tools}
            async def call(name, arguments, label, error=False):
                result = await session.call_tool(name, arguments)
                text = "\n".join(getattr(item, "text", "") for item in result.content)
                assert bool(result.isError) == error, label
                results[label] = {"tool": name, "arguments": arguments, "is_error": bool(result.isError), "text": text}
                if name == "find_code" and result.structuredContent is not None:
                    return result.structuredContent
                if name == "find_code" and not error:
                    return json.loads(text)
                return text
            assert repository.commit_sha in await call("set_repo", {"repo_url": URL}, "select_repository")
            for label, arguments in (
                ("path", dict(query="config.py", mode="path")),
                ("symbol", dict(query="_get_agent", mode="symbol")),
                ("text_config", dict(query="OPENAI_MODEL =", path_filter="config.py")),
                ("text_import", dict(query="from dotenv import", path_filter="config.py")),
            ):
                data = await call("find_code", arguments, label)
                assert data["hits"] and data["commit_sha"] == repository.commit_sha
            assert not (await call("find_code", {"query": "__ABSENT_LOCAL_FIXTURE_LITERAL__"}, "no_match"))["hits"]
            page = dict(query="def ", path_filter="agent/qa_agent.py", limit=1)
            first = await call("find_code", page, "first_page")
            assert first["next_offset"] == 1
            second = await call("find_code", page | {"offset": first["next_offset"]}, "next_page")
            assert first["hits"][0]["start_line"] != second["hits"][0]["start_line"]
            assert "error" in await call("find_code", {"query": ""}, "empty_query")
            await call("find_code", {"query": "x", "mode": "regex"}, "invalid_mode", error=True)
            await call("find_code", {"query": "x", "limit": True}, "boolean_limit", error=True)
            await call("set_repo", {"repo_url": "file:///invalid"}, "invalid_selection")
            assert "error" in await call("find_code", {"query": "OPENAI_MODEL"}, "cleared_context")
            return {"tool_count": len(tools.tools), "calls_passed": len(results), "results": results}


async def main():
    models.ALLOW_MODEL_REQUESTS = False
    repository = get_current_repository(URL)
    assert repository is not None, "Prepare the recorded snapshot/index first."
    report = {"status": "passed", "chat_model_called": False, "repo_url": URL,
              "commit_sha": repository.commit_sha, "index_collection": repository.collection_name,
              "native": check_native(repository), "framework": await check_framework(repository),
              "mcp": await check_mcp(repository)}
    assert get_current_repository(URL) == repository
    report["active_index_unchanged"] = True
    report["limits"] = dict(file_bytes=MAX_FILE_BYTES, scan_bytes=MAX_SCAN_BYTES, scan_files=MAX_SCAN_FILES)
    return report


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/checks/20261110-code-search.json"))
    destination = parser.parse_args().output.resolve()
    if destination.exists():
        raise RuntimeError("Choose a new --output; saved evidence is not overwritten.")
    report = asyncio.run(asyncio.wait_for(main(), 120))
    paths = ("indexer/code_search.py", "indexer/source_reader.py", "agent/tools.py", "agent/qa_agent.py", "mcp_server.py", "scripts/check_code_search.py", "config.py")
    report["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "chat_model_called": False, "framework_turns": report["framework"]["model_turns"],
                      "mcp_calls": report["mcp"]["calls_passed"], "active_index_unchanged": report["active_index_unchanged"]}))
