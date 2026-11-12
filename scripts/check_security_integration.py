"""Read the existing index through a real MCP stdio process, without importing."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from indexer.repository_service import get_current_repository
from indexer.snapshots import load_snapshot
from security.output import redact_data

URL = "https://github.com/xyma2003/codebase-qa"


async def check():
    before = get_current_repository(URL)
    if before is None:
        raise ValueError("This check requires the existing ready index; it never imports or rebuilds.")
    results = {}
    environment = {key:os.environ[key] for key in ("PATH","HOME","TMPDIR","LANG","HF_HOME") if key in os.environ}
    environment.update(HF_HUB_OFFLINE="1", CODEBASE_QA_MCP_ALLOW_IMPORT="0", OPENAI_API_KEY="", ANTHROPIC_API_KEY="")
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/"mcp_server.py")], cwd=str(ROOT), env=environment)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {"set_repo","list_repo_versions","get_repo_overview","get_file_dependencies","find_code",
                             "search_code","list_repo_files","read_file","verify_source_citation"}
            async def call(name, arguments, label):
                response = await session.call_tool(name, arguments)
                assert not response.isError, label
                text = "\n".join(getattr(block,"text","") for block in response.content)
                results[label] = {"tool":name,"arguments":arguments,"text":text}
                return text
            assert before.commit_sha in await call("set_repo", {"repo_url":URL}, "select_existing")
            assert before.commit_sha in await call("list_repo_versions", {}, "versions")
            assert "config.py" in await call("list_repo_files", {"pattern":"config.py"}, "files")
            assert "1 | import os" in await call("read_file", {"file_path":"config.py","start_line":1,"end_line":2}, "read")
            source = load_snapshot(before.repo_root).read("config.py",1,2)
            assert '"valid": true' in await call("verify_source_citation", {"file_path":"config.py","start_line":1,"end_line":2,
                 "source_text":source.content,"commit_sha":before.commit_sha}, "verify")
            assert "config.py" in await call("find_code", {"query":"config.py","mode":"path"}, "find")
            assert "repo_url" in await call("get_repo_overview", {}, "overview")
            assert "file_path" in await call("get_file_dependencies", {"file_path":"app.py"}, "dependencies")
            assert "lines " in await call("search_code", {"query":"configuration","use_answer_cache":False}, "search")
            assert "敏感文件" in await call("read_file", {"file_path":".env"}, "secret_path_denied")
            assert "repository-relative" in await call("read_file", {"file_path":"../config.py"}, "traversal_denied")
            assert "disabled" in await call("set_repo", {"repo_url":URL,"refresh":True}, "network_refresh_denied")
            assert "No repo set" in await call("read_file", {"file_path":"config.py"}, "failed_selection_cleared")
            assert before.commit_sha in await call("set_repo", {"repo_url":URL}, "restore_existing")
    assert before == get_current_repository(URL)
    return {"status":"mcp_security_integration_passed","protocol":initialized.protocolVersion,"tool_count":len(names),
            "calls_passed":len(results),"active_source_commit":before.commit_sha,"active_chunks":before.chunk_count,
            "index_rebuilt":False,"real_model_requests":0,"network_import_enabled":False,"results":redact_data(results)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose an independent output file")
    result = asyncio.run(asyncio.wait_for(check(), timeout=120))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as file:
        file.write(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({key:value for key,value in result.items() if key != "results"}, ensure_ascii=False))
