"""Independent processes, actual Git/Chroma/native messages, no model calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
URL = "https://github.com/example/operations-fixture"


def fixture(root, operation, expected):
    os.chdir(root)
    from agent import conversations
    from agent.evidence import CheckedAnswer
    from indexer import repository_service as service, repository_state, snapshots, vector_store
    from security.identity import Principal, as_principal, user_id
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
    actors = {name: Principal(user_id("https://identity.example.invalid", name), "user") for name in ("alice", "bob")}
    if operation == "seed":
        from git import Actor, Repo
        work = root / "repos/source"
        repo = Repo.init(work)
        (work / "sample.py").write_text("def answer():\n    return 42\n")
        repo.index.add(["sample.py"])
        actor = Actor("Fixture", "fixture@example.invalid")
        sha = repo.index.commit("restore fixture", author=actor, committer=actor).hexsha
        repo.close()
        snapshot = snapshots.create_snapshot(URL, str(work), sha)
        collection = vector_store._get_client().create_collection("repo-restore-fixture", embedding_function=None,
            metadata={"repo_url": URL, "commit_sha": sha})
        collection.add(ids=["sample.py:1"], embeddings=[[.1, .2, .3]], documents=["def answer():\n    return 42"],
            metadatas=[{"file_path": "sample.py", "start_line": 1, "end_line": 2, "commit_sha": sha}])
        record = dict(repo_url=URL, commit_sha=sha, repo_root=snapshot.root, collection_name=collection.name,
            chunk_count=1, file_count=1, created_at="2026-11-17T00:00:00Z", fingerprint=service._fingerprint(), reused_legacy_vectors=False)
        repository_state.publish(record, None)
        sessions = {}
        for name, principal in actors.items():
            with as_principal(principal):
                session = conversations.create_conversation(URL, sha, collection.name)
                conversation = conversations.load_conversation(session, URL, sha, collection.name)
                question, answer = "LOCAL_" + name, "RESTORE_LOCAL_" + name
                messages = [ModelRequest(parts=[UserPromptPart(question)]), ModelResponse(parts=[TextPart(answer)])]
                conversations.append_turn(conversation, question, messages, CheckedAnswer(text=answer), 0)
                sessions[name] = session
        expected.write_text(json.dumps({"sha": sha, "sessions": sessions}, indent=2))
    else:
        saved = json.loads(expected.read_text())
        repository = service.get_current_repository(URL)
        assert repository.commit_sha == saved["sha"] and repository.chunk_count == 1
        stored = vector_store._get_client().get_collection(repository.collection_name, embedding_function=None).get(include=["embeddings", "documents"])
        assert stored["ids"] == ["sample.py:1"] and "return 42" in stored["documents"][0]
        assert abs(float(stored["embeddings"][0][0]) - .1) < .00001
        for name, principal in actors.items():
            with as_principal(principal):
                history = conversations.load_conversation(saved["sessions"][name], URL, repository.commit_sha, repository.collection_name)
                assert history.revision == 1 and len(history.messages) == 2
                other = "bob" if name == "alice" else "alice"
                try:
                    conversations.load_conversation(saved["sessions"][other], URL, repository.commit_sha, repository.collection_name)
                except ValueError:
                    pass
                else:
                    raise AssertionError("CROSS_OWNER_RESTORE_ACCESS")
        print("RESTORED_NATIVE_MESSAGES_VECTOR_SOURCE_AND_OWNERS_OK")


def main(output):
    from ops.backup import backup, restore
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="codebase-qa-restore-") as temp:
        root = Path(temp) / "runtime"
        root.mkdir()
        expected = output / "fixture-expected.json"
        def child(operation):
            result = subprocess.run([sys.executable, __file__, "--worker", operation, "--root", str(root), "--expected", str(expected)],
                cwd=ROOT, capture_output=True, text=True, timeout=60, env=dict(os.environ, HF_HUB_OFFLINE="1"))
            (output / (operation + ".log")).write_text(result.stdout + result.stderr)
            if result.returncode:
                raise RuntimeError(operation + " failed; inspect the independent process log")
        child("seed")
        archive = output / "state.zip"
        saved = backup(root, archive, service_stopped=True)
        # The original path is unavailable for verification; all reads must come
        # from restored files. Preserve the original elsewhere until drill ends.
        root.rename(root.with_name("original-not-used"))
        loaded = restore(archive, root, runtime_root=root)
        child("verify")
        report = {"status": "stopped_service_restore_passed", "scope": "temporary_git_chroma_and_two_user_fixture",
            "source_process_exited_before_backup": True, "original_path_replaced_for_readback": True,
            "source_snapshot_checked": True, "vectors_restored": 1, "conversations_restored": 2,
            "native_turns_restored": 2, "cross_owner_denied": True, "model_requests": 0,
            "real_business_data_modified": False, "backup": saved, "restore": {"files": loaded["files"]}}
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=("seed", "verify"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--expected", type=Path)
    args = parser.parse_args()
    if args.worker:
        fixture(args.root, args.worker, args.expected)
    else:
        if not args.output:
            parser.error("--output is required")
        main(args.output.absolute())
