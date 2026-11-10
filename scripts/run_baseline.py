"""Observe the unchanged application and save stage-0 evidence.

Run from the repository root with the project's virtual environment.
No model, prompt, retrieval, or memory setting is overridden.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPO_URL = "https://github.com/xyma2003/codebase-qa"
BASE_SHA = "7ed1f8f22e3c1caf1c23b9ca976bf9e86a5feffb"
QUESTION = "这个项目如何从 GitHub 仓库地址建立索引？请指出主要函数、文件和执行顺序。"


def command(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def redact(text: str) -> str:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    path.write_text(redact(data), encoding="utf-8")


def configuration() -> dict:
    import config

    names = (
        "LLM_PROVIDER", "LLM_MODEL_STRING", "OPENAI_API_BASE", "OPENAI_MODEL",
        "CLAUDE_MODEL", "CHROMA_DIR", "CLONE_DIR", "EMBED_MODEL",
        "MAX_CHUNK_TOKENS", "MEMORY_SIMILARITY_THRESHOLD", "QUALITY_THRESHOLD",
    )
    effective = {name: getattr(config, name) for name in names}
    effective["CODE_EXTENSIONS"] = sorted(config.CODE_EXTENSIONS)
    effective["credentials_present"] = {
        name: bool(getattr(config, name))
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    }
    return effective


def inspect(output: Path) -> dict:
    files = [
        name for name in command("git", "ls-tree", "-r", "--name-only", BASE_SHA).splitlines()
        if name.endswith(".py") or name in ("requirements.txt", ".streamlit/config.toml")
    ]
    checksums = {}
    for name in files:
        data = (ROOT / name).read_bytes()
        original = subprocess.check_output(["git", "show", f"{BASE_SHA}:{name}"], cwd=ROOT)
        if data != original:
            raise RuntimeError(f"Application source changed from baseline: {name}")
        checksums[name] = hashlib.sha256(data).hexdigest()
    packages = sorted(
        f"{d.metadata['Name']}=={d.version}"
        for d in importlib.metadata.distributions()
        if d.metadata.get("Name")
    )
    (output / "requirements-resolved.txt").write_text("\n".join(packages) + "\n")
    return {
        "source_sha": BASE_SHA,
        "checkout_head": command("git", "rev-parse", "HEAD"),
        "source_matches_baseline": True,
        "source_sha256": checksums,
        "python": sys.version,
        "platform": platform.platform(),
        "requested_model": {"provider": "SiliconFlow", "model": "Qwen/Qwen3-32B"},
        "effective_configuration": configuration(),
        "runtime_environment": {
            name: os.environ.get(name)
            for name in ("HF_HOME", "UV_CACHE_DIR", "TOKENIZERS_PARALLELISM")
        },
        "package_count": len(packages),
        "question": QUESTION,
        "target_repository": REPO_URL,
        "target_sha_expected": BASE_SHA,
    }


def index(output: Path) -> dict:
    from indexer import clone_repo, chunk_repo, index_chunks, is_indexed
    from indexer.vector_store import search, list_files
    from indexer import vector_store

    repo_root = Path(clone_repo(REPO_URL))
    sha = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
    if sha != BASE_SHA:
        raise RuntimeError(f"Target changed: expected {BASE_SHA}, got {sha}")
    chunks = list(chunk_repo(str(repo_root)))
    if not chunks:
        raise RuntimeError("No chunks produced")
    for chunk in chunks:
        lines = (repo_root / chunk.file_path).read_text(errors="ignore").splitlines()
        if "\n".join(lines[chunk.start_line - 1:chunk.end_line]) != chunk.content:
            raise RuntimeError(f"Chunk line mismatch: {chunk.file_path}:{chunk.start_line}")
    reused = is_indexed(REPO_URL)
    if not reused:
        index_chunks(chunks, REPO_URL)
    collection = vector_store._get_client().get_collection(
        name=vector_store._get_collection_name(REPO_URL)
    )
    count = collection.count()
    if count != len(chunks):
        raise RuntimeError(f"Index count mismatch: {count} != {len(chunks)}")
    stored = collection.get(include=["documents", "metadatas"])
    expected = {f"{chunk.file_path}:{chunk.start_line}": chunk for chunk in chunks}
    if set(stored["ids"]) != set(expected):
        raise RuntimeError("Persisted chunk identities differ from input")
    for chunk_id, document, metadata in zip(stored["ids"], stored["documents"], stored["metadatas"]):
        chunk = expected[chunk_id]
        if document != chunk.content or metadata["end_line"] != chunk.end_line:
            raise RuntimeError(f"Persisted chunk differs from input: {chunk_id}")
    results = search(QUESTION, REPO_URL, n_results=5)
    if not results or not all(result["content"] for result in results):
        raise RuntimeError("Search returned no nonempty chunks")
    save(output / "retrieval-results.json", results)
    return {
        "target_sha": sha,
        "chunk_count": len(chunks),
        "indexed_chunk_count": count,
        "file_count": len(list_files(REPO_URL)),
        "chunk_line_checks": "passed",
        "persisted_content_readback": "passed",
        "reused_existing_index": reused,
        "retrieval_count": len(results),
        "retrieval_quality": "not_scored",
    }


async def mcp_probe(output: Path) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=[str(ROOT / "mcp_server.py")],
        env=dict(os.environ), cwd=str(ROOT),
    )
    observations = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            available = await session.list_tools()
            names = sorted(tool.name for tool in available.tools)
            if names != ["list_repo_files", "read_file", "search_code", "set_repo"]:
                raise RuntimeError(f"Unexpected MCP tools: {names}")
            calls = [
                ("set_repo", {"repo_url": REPO_URL}),
                ("list_repo_files", {"pattern": "config.py"}),
                ("read_file", {"file_path": "config.py"}),
                ("search_code", {"query": QUESTION}),
            ]
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                value = result.model_dump(mode="json")
                observations[name] = value
                if result.isError:
                    raise RuntimeError(f"MCP call failed: {name}")
                text = "\n".join(getattr(c, "text", "") for c in result.content)
                if not text or "No repo set" in text or "No relevant code" in text:
                    raise RuntimeError(f"Empty or missing MCP result: {name}")
            save(output / "mcp-results.json", observations)
            return {"tools": names, "calls_passed": len(calls), "protocol": initialized.protocolVersion}


def ui_probe(output: Path) -> dict:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=60)
    if len(app.exception):
        raise RuntimeError(str(app.exception[0].value))
    app.text_input[0].set_value(REPO_URL).run(timeout=60)
    if len(app.exception):
        raise RuntimeError(str(app.exception[0].value))
    return {
        "test": "Streamlit AppTest",
        "initial_render": "passed",
        "indexed_repository_render": "passed",
        "titles": [item.value for item in app.title],
        "success_messages": [item.value for item in app.success],
        "model_question_submitted": False,
    }


async def ask_probe(output: Path) -> dict:
    effective = configuration()
    if not effective["credentials_present"]["OPENAI_API_KEY"]:
        return {
            "status": "blocked",
            "reason": "User-selected SiliconFlow API key is missing; no model request sent.",
            "effective_configuration": effective,
        }
    if effective["LLM_PROVIDER"] != "openai" or effective["OPENAI_MODEL"] != "Qwen/Qwen3-32B":
        raise RuntimeError("Effective provider/model differs from the user-selected configuration")
    from pydantic_ai import capture_run_messages
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from agent.qa_agent import ask_stream
    from indexer.cloner import clone_repo

    with capture_run_messages() as messages:
        parts = [part async for part in ask_stream(QUESTION, REPO_URL, clone_repo(REPO_URL))]
    answer = "".join(parts)
    if not answer.strip():
        raise RuntimeError("Original ask_stream returned an empty answer")
    (output / "answer.txt").write_text(redact(answer) + "\n")
    save(output / "model-messages.json", json.loads(ModelMessagesTypeAdapter.dump_json(messages)))
    return {
        "question": QUESTION, "answer_chars": len(answer),
        "output_chunks": len(parts), "original_entrypoint": "agent.qa_agent.ask_stream",
        "answer_quality": "requires_review", "effective_configuration": effective,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["inspect", "index", "mcp", "ui", "ask"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.chdir(ROOT)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    record = {"phase": args.phase, "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
    try:
        if args.phase == "inspect": result = inspect(output)
        elif args.phase == "index": result = index(output)
        elif args.phase == "mcp": result = asyncio.run(asyncio.wait_for(mcp_probe(output), timeout=180))
        elif args.phase == "ui": result = ui_probe(output)
        else: result = asyncio.run(ask_probe(output))
        record.update(status=result.pop("status", "passed"), result=result)
    except Exception as exc:
        record.update(status="failed", error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    save(output / f"{args.phase}.json", record)
    print(redact(json.dumps({k: v for k, v in record.items() if k != "traceback"}, ensure_ascii=False, default=str)))
    return 1 if record["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
