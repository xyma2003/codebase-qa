"""Check the real MCP interface without a chat-model request or reindexing."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/xyma2003/codebase-qa"
TARGET = ROOT / "repos/xyma2003__codebase-qa"


async def check():
    if not TARGET.is_dir():
        raise RuntimeError("Run the fixed stage-0 repository import first.")
    results = {}
    params = StdioServerParameters(
        command=sys.executable, args=[str(ROOT / "mcp_server.py")],
        env=dict(os.environ), cwd=str(ROOT),
    )
    with tempfile.NamedTemporaryFile(mode="w", dir=TARGET.parent, prefix="source-reader-", delete=False) as fixture:
        fixture.write("OUTSIDE_SOURCE_FIXTURE")
        outside = Path(fixture.name)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                reader = next(tool for tool in tools.tools if tool.name == "read_file")
                assert {"file_path", "start_line", "end_line"} <= set(reader.inputSchema["properties"])

                async def call(name, arguments, label):
                    response = await session.call_tool(name, arguments)
                    assert not response.isError, label
                    text = "\n".join(getattr(item, "text", "") for item in response.content)
                    results[label] = {"tool": name, "arguments": arguments, "text": text}
                    return text

                text = await call("set_repo", {"repo_url": REPO_URL}, "select_existing_repository")
                assert "Active repo:" in text
                text = await call("list_repo_files", {"pattern": "config.py"}, "list_files")
                assert "config.py" in text
                text = await call("read_file", {"file_path": "config.py"}, "legacy_read_call")
                assert "1 | import os" in text
                text = await call("read_file", {"file_path": "config.py", "start_line": 10, "end_line": 15}, "numbered_range")
                original = (TARGET / "config.py").read_text().splitlines()
                for number in range(10, 16):
                    assert f"{number} | {original[number - 1]}" in text
                assert "9 | " not in text and "16 | " not in text
                text = await call("read_file", {"file_path": f"../{outside.name}"}, "outside_repository")
                assert "Only repository-relative paths" in text and "OUTSIDE_SOURCE_FIXTURE" not in text
                text = await call("read_file", {"file_path": "config.py", "start_line": 0}, "invalid_line_range")
                assert "start_line must be a positive integer" in text
                text = await call("search_code", {"query": "How are repository code chunks indexed?"}, "existing_search")
                assert "lines " in text and "No relevant code" not in text
                return {
                    "status": "passed", "protocol": initialized.protocolVersion,
                    "tool_count": len(tools.tools), "tool_calls_passed": len(results),
                    "chat_model_called": False, "index_rebuilt": False,
                    "source_sha256": {
                        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ("indexer/source_reader.py", "agent/tools.py", "mcp_server.py")
                    },
                    "results": results,
                }
    finally:
        outside.unlink()


if __name__ == "__main__":
    result = asyncio.run(asyncio.wait_for(check(), timeout=180))
    destination = ROOT / "docs/checks/20261110-source-reader.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "results"}, ensure_ascii=False))
