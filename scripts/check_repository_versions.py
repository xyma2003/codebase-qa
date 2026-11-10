"""Real Chroma and Git checks; no paid/chat model requests."""

import asyncio
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import chromadb
from git import Actor, Repo
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from indexer import repository_service as service, repository_state as state, snapshots, vector_store

URL = "https://github.com/xyma2003/codebase-qa"


def migrate_existing():
    client = vector_store._get_client()
    legacy_name = vector_store._get_collection_name(URL)
    legacy = client.get_collection(legacy_name, embedding_function=None)
    original = legacy.get(include=["documents", "metadatas", "embeddings"])
    function = vector_store._get_embed_fn()
    with patch.object(vector_store, "_get_embed_fn", return_value=function), patch.object(
        function._model, "encode", side_effect=AssertionError("Existing vectors must not be recomputed")
    ) as encode:
        current = service.import_repository(URL)
        assert current.reused_legacy_vectors
        assert encode.call_count == 0
    remaining = legacy.get(include=["documents", "metadatas", "embeddings"])
    assert original["ids"] == remaining["ids"]
    assert original["documents"] == remaining["documents"]
    assert original["metadatas"] == remaining["metadatas"]
    assert original["embeddings"].tobytes() == remaining["embeddings"].tobytes()
    return {"commit_sha": current.commit_sha, "chunks": current.chunk_count, "files": current.file_count,
            "legacy_vectors_reused": True, "embedding_encode_calls": encode.call_count,
            "legacy_data_unchanged": True, "collection_name": current.collection_name}


def native_versions():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        work = root / "work"
        with Repo.init(work) as repo:
            actor = Actor("Fixture", "fixture@example.invalid")
            file = work / "example.py"
            def commit(value):
                file.write_text(f"def value():\n    return {value}\n")
                repo.index.add(["example.py"])
                return repo.index.commit(f"value {value}", author=actor, committer=actor).hexsha
            first = commit(1)
            client = chromadb.PersistentClient(path=str(root / "vectors"))
            fixture_url = "https://github.com/example/native-fixture"
            with patch.object(snapshots, "CLONE_DIR", str(root / "snapshots")), patch.object(state, "CHROMA_DIR", str(root / "metadata")), patch.object(service, "clone_repo", return_value=str(work)), patch.object(service, "_fetch_default_commit", side_effect=lambda _: repo.head.commit.hexsha), patch.object(vector_store, "_get_client", return_value=client):
                old = service.import_repository(fixture_url)
                second = commit(2)
                new = service.import_repository(fixture_url, refresh=True)
                old_result = vector_store.search("value", fixture_url, 1, collection_name=old.collection_name)
                new_result = vector_store.search("value", fixture_url, 1, collection_name=new.collection_name)
                assert "return 1" in old_result[0]["content"] and old_result[0]["commit_sha"] == first
                assert "return 2" in new_result[0]["content"] and new_result[0]["commit_sha"] == second
                assert "return 1" in snapshots.load_snapshot(old.repo_root).read("example.py").content
                assert service.import_repository(fixture_url, commit_sha=first) == old
                assert service.get_current_repository(fixture_url) == new
                commit(3)
                def fail_after_write(done, total):
                    raise RuntimeError("FIXTURE_CALLBACK_FAILURE")
                try:
                    service.import_repository(fixture_url, refresh=True, progress_callback=fail_after_write)
                except RuntimeError as exc:
                    assert str(exc) == "FIXTURE_CALLBACK_FAILURE"
                else:
                    raise AssertionError("Expected the controlled build failure")
                assert service.get_current_repository(fixture_url) == new
                assert len(client.list_collections()) == 2
                return {"old_sha": first, "new_sha": second, "old_and_new_search_match": True,
                        "old_source_retained": True, "failed_build_keeps_active": True,
                        "partial_collection_removed": True, "ready_versions": 2}


async def protocol_checks():
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], env=dict(os.environ), cwd=str(ROOT))
    results = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            assert "list_repo_versions" in {tool.name for tool in tools.tools}
            async def call(name, arguments, label):
                result = await session.call_tool(name, arguments)
                assert not result.isError, label
                text = "\n".join(getattr(part, "text", "") for part in result.content)
                results[label] = {"tool": name, "arguments": arguments, "text": text}
                return text
            current = service.get_current_repository(URL)
            assert current.commit_sha in await call("set_repo", {"repo_url": URL}, "select_version")
            assert current.commit_sha in await call("list_repo_versions", {}, "list_versions")
            text = await call("read_file", {"file_path": "config.py", "start_line": 10, "end_line": 15}, "read_pinned_source")
            assert f"Commit: {current.commit_sha}" in text and "10 | " in text
            text = await call("search_code", {"query": "How are code chunks indexed?"}, "search_pinned_collection")
            assert f"Commit: {current.commit_sha}" in text and "lines " in text
            text = await call("set_repo", {"repo_url": "file:///not-a-github-repository"}, "invalid_repository")
            assert "Failed to select repository" in text
            assert "No repo set" in await call("read_file", {"file_path": "config.py"}, "failed_selection_clears_context")
            assert current.commit_sha in await call("set_repo", {"repo_url": URL, "commit_sha": current.commit_sha}, "explicit_version_selection")
            return {"protocol": initialized.protocolVersion, "tool_count": len(tools.tools),
                    "calls_passed": len(results), "results": results}


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/checks/20261110-repository-versions.json"))
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.exists():
        raise RuntimeError("Existing verification evidence is immutable; choose a new --output before rerunning.")
    report = {"status": "passed", "chat_model_called": False, "migration": migrate_existing(),
              "native_storage": native_versions(), "mcp": asyncio.run(asyncio.wait_for(protocol_checks(), 180))}
    report["source_sha256"] = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in ("app.py", "mcp_server.py", "agent/tools.py", "agent/qa_agent.py", "memory/knowledge.py",
                     "indexer/snapshots.py", "indexer/repository_service.py", "indexer/repository_state.py", "indexer/vector_store.py")
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "migration": report["migration"], "native_storage": report["native_storage"],
                      "mcp_calls_passed": report["mcp"]["calls_passed"], "chat_model_called": False}, ensure_ascii=False))
