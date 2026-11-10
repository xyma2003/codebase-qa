import asyncio
from dataclasses import asdict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from git import Actor, Repo

from indexer import repository_service as service, repository_state as state, snapshots
from indexer.chunker import chunk_repo
from memory.knowledge import _memory_collection_name

URL = "https://github.com/example/fixture"
ACTOR = Actor("Fixture", "fixture@example.invalid")


class FakeCollection:
    def __init__(self, chunks, url=URL, commit_sha=None):
        self.metadata = {"repo_url": url}
        self.configuration_json = {"embedding_function": {"config": {"model_name": "all-MiniLM-L6-v2"}}}
        self.values = {
            "ids": [f"{c.file_path}:{c.start_line}" for c in chunks],
            "documents": [c.content for c in chunks],
            "metadatas": [{key: getattr(c, key) for key in ("file_path", "start_line", "end_line", "chunk_type", "name")} for c in chunks],
            "embeddings": [[1.0, 0.0] for _ in chunks],
        }
        if commit_sha:
            self.metadata["commit_sha"] = commit_sha
            for metadata in self.values["metadatas"]:
                metadata["commit_sha"] = commit_sha

    def get(self, include=None):
        return self.values

    def count(self):
        return len(self.values["ids"])


class FakeClient:
    def __init__(self):
        self.collections = {}
        self.deleted = []

    def list_collections(self):
        return [SimpleNamespace(name=name) for name in self.collections]

    def get_collection(self, name, **kwargs):
        return self.collections[name]

    def delete_collection(self, name):
        self.deleted.append(name)
        del self.collections[name]


class RepositoryVersionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / "work"
        self.repo = Repo.init(self.work)
        self.addCleanup(self.repo.close)
        self.file = self.work / "example.py"
        self.first = self.commit("def value():\n    return 1\n")
        self.client = FakeClient()
        self.builds = []
        patches = [
            patch.object(snapshots, "CLONE_DIR", str(self.root / "repos")),
            patch.object(state, "CHROMA_DIR", str(self.root / "state")),
            patch.object(service, "clone_repo", return_value=str(self.work)),
            patch.object(service, "_fetch_default_commit", side_effect=lambda _: self.repo.head.commit.hexsha),
            patch.object(service.vector_store, "_get_client", return_value=self.client),
            patch.object(service.vector_store, "_get_embed_fn", return_value=SimpleNamespace(get_config=lambda: {"model_name": "all-MiniLM-L6-v2"})),
            patch.object(service.vector_store, "build_collection", side_effect=self.build),
        ]
        for context in patches:
            context.start()
            self.addCleanup(context.stop)

    def commit(self, content):
        self.file.write_text(content)
        self.repo.index.add(["example.py"])
        return self.repo.index.commit(content, author=ACTOR, committer=ACTOR).hexsha

    def build(self, chunks, url, name, sha, embeddings, callback):
        self.client.collections[name] = FakeCollection(chunks, url, sha)
        self.builds.append({"name": name, "sha": sha, "embeddings": embeddings})

    def test_snapshot_reads_committed_files_not_untracked_or_dirty_content(self):
        self.file.write_text("LOCAL_DIRTY_CONTENT")
        (self.work / ".env").write_text("PRIVATE_UNTRACKED_FIXTURE")
        snapshot = snapshots.create_snapshot(URL, str(self.work))
        self.assertEqual(snapshot.commit_sha, self.first)
        self.assertIn("return 1", snapshot.read("example.py").content)
        self.assertNotIn(".env", snapshot.files)

    def test_old_snapshot_survives_a_new_commit(self):
        old = snapshots.create_snapshot(URL, str(self.work))
        second = self.commit("def value():\n    return 2\n")
        new = snapshots.create_snapshot(URL, str(self.work))
        self.assertNotEqual(old.root, new.root)
        self.assertEqual(new.commit_sha, second)
        self.assertIn("return 1", old.read("example.py").content)
        self.assertIn("return 2", new.read("example.py").content)

    def test_tampered_snapshot_is_rejected(self):
        snapshot = snapshots.create_snapshot(URL, str(self.work))
        file = Path(snapshot.root) / "example.py"
        file.chmod(0o644)
        file.write_text("CHANGED_SNAPSHOT")
        with self.assertRaises(ValueError):
            snapshot.read("example.py")
        with self.assertRaises(ValueError):
            snapshots.create_snapshot(URL, str(self.work))

    def test_git_symlinks_are_not_materialized(self):
        (self.work / "linked.py").symlink_to("example.py")
        self.repo.index.add(["linked.py"])
        self.repo.index.commit("add link", author=ACTOR, committer=ACTOR)
        snapshot = snapshots.create_snapshot(URL, str(self.work))
        self.assertIn("linked.py", snapshot.skipped)
        self.assertFalse((Path(snapshot.root) / "linked.py").exists())

    def test_nonportable_git_paths_are_rejected(self):
        entry = SimpleNamespace(type="blob", path="C:\\escape.py")
        with patch("git.objects.tree.Tree.traverse", return_value=iter([entry])):
            with self.assertRaisesRegex(ValueError, "不合法路径"):
                snapshots.create_snapshot(URL, str(self.work))

    def test_normalized_identity_and_invalid_urls(self):
        self.assertEqual(snapshots.normalize_repo_url(" https://github.com/Example/Fixture.git/ "), URL)
        for url in ("file:///tmp/repo", "https://other.test/a/b", "https://token@github.com/a/b", "https://github.com/a/b/tree/main", "https://github.com/a/b?token=x", "https://github.com/a/.."):
            with self.subTest(url=url), self.assertRaises(ValueError):
                snapshots.normalize_repo_url(url)

    def test_default_import_reuses_version_without_fetch_or_rebuild(self):
        first = service.import_repository(URL)
        with patch.object(service, "_fetch_default_commit") as fetch:
            second = service.import_repository(URL)
            fetch.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(len(self.builds), 1)

    def test_refresh_and_explicit_selection_keep_old_version_usable(self):
        old = service.import_repository(URL)
        second = self.commit("def value():\n    return 2\n")
        new = service.import_repository(URL, refresh=True)
        self.assertEqual(new.commit_sha, second)
        self.assertEqual(service.get_current_repository(URL), new)
        selected = service.import_repository(URL, commit_sha=old.commit_sha)
        self.assertEqual(selected, old)
        self.assertEqual(service.get_current_repository(URL), new)
        self.assertIn(old.collection_name, self.client.collections)
        self.assertEqual(len(service.list_repository_versions(URL)), 2)

    def test_same_remote_commit_does_not_reindex(self):
        first = service.import_repository(URL)
        self.assertEqual(service.import_repository(URL, refresh=True), first)
        self.assertEqual(len(self.builds), 1)

    def test_missing_published_collection_is_not_reported_ready(self):
        first = service.import_repository(URL)
        self.client.delete_collection(first.collection_name)
        self.assertIsNone(service.get_current_repository(URL))
        self.assertEqual(service.list_repository_versions(URL), [])
        replacement = service.import_repository(URL)
        self.assertNotEqual(replacement.collection_name, first.collection_name)
        self.assertEqual(replacement.commit_sha, first.commit_sha)

    def test_storage_version_mismatch_is_rejected(self):
        first = service.import_repository(URL)
        self.client.collections[first.collection_name].metadata["commit_sha"] = "wrong-version"
        with self.assertRaisesRegex(ValueError, "索引存储"):
            service.get_current_repository(URL)

    def test_failed_build_preserves_ready_index_and_cleans_only_staging(self):
        old = service.import_repository(URL)
        self.commit("def value():\n    return 2\n")
        def failed(*args):
            self.build(*args)
            raise RuntimeError("FIXTURE_BUILD_FAILURE")
        with patch.object(service.vector_store, "build_collection", side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, "FIXTURE_BUILD_FAILURE"):
                service.import_repository(URL, refresh=True)
        self.assertEqual(service.get_current_repository(URL), old)
        self.assertEqual(set(self.client.collections), {old.collection_name})
        self.assertNotIn(old.collection_name, self.client.deleted)

    def test_legacy_vectors_reused_only_after_content_and_config_match(self):
        name = service.vector_store._get_collection_name(URL)
        legacy = FakeCollection(list(chunk_repo(str(self.work))))
        self.client.collections[name] = legacy
        version = service.import_repository(URL)
        self.assertTrue(version.reused_legacy_vectors)
        self.assertEqual(self.builds[0]["embeddings"], [[1.0, 0.0]])
        self.assertIn(name, self.client.collections)

    def test_mismatched_legacy_source_is_not_reused(self):
        name = service.vector_store._get_collection_name(URL)
        legacy = FakeCollection(list(chunk_repo(str(self.work))))
        legacy.values["documents"][0] = "STALE_INDEX_CONTENT"
        self.client.collections[name] = legacy
        self.assertFalse(service.import_repository(URL).reused_legacy_vectors)
        self.assertIsNone(self.builds[0]["embeddings"])

    def test_old_concurrent_publish_cannot_replace_newer_active_index(self):
        old = service.import_repository(URL)
        self.commit("def value():\n    return 2\n")
        new = service.import_repository(URL, refresh=True)
        stale = asdict(old)
        stale["collection_name"] = "older-concurrent-build"
        state.publish(stale, old.collection_name)
        self.assertEqual(service.get_current_repository(URL), new)

    def test_memory_identity_separates_repositories_and_commits(self):
        names = {
            _memory_collection_name(URL), _memory_collection_name(URL, self.first),
            _memory_collection_name(URL, "b" * 40),
            _memory_collection_name("https://github.com/other/fixture", self.first),
        }
        self.assertEqual(len(names), 4)
        self.assertTrue(all(len(name) <= 63 for name in names))

    def test_agent_forwards_pinned_version_to_tools_and_memory(self):
        from agent import qa_agent
        snapshot = snapshots.create_snapshot(URL, str(self.work))
        async def respond(question, *, deps, **kwargs):
            deps.evidence.read("example.py")
            return SimpleNamespace(output="fixture answer [S1]")
        run = AsyncMock(side_effect=respond)
        with patch.object(qa_agent, "_get_agent", return_value=SimpleNamespace(run=run)), patch.object(qa_agent, "evaluate", return_value=1), patch.object(qa_agent, "save_qa") as save:
            asyncio.run(qa_agent.ask("question", URL, snapshot.root, collection_name="fixed-collection", commit_sha=self.first))
            deps = run.call_args.kwargs["deps"]
            self.assertEqual((deps.collection_name, deps.commit_sha), ("fixed-collection", self.first))
            self.assertEqual(save.call_args.kwargs["commit_sha"], self.first)

    def test_agent_cannot_pin_only_half_of_a_repository_context(self):
        from agent.tools import RepoDeps
        with self.assertRaises(ValueError):
            RepoDeps(URL, "root", commit_sha=self.first)
        with self.assertRaises(ValueError):
            RepoDeps(URL, "root", collection_name="collection")

    def test_stream_entrypoint_keeps_the_same_pinned_version(self):
        from agent import qa_agent
        snapshot = snapshots.create_snapshot(URL, str(self.work))
        async def respond(question, *, deps, **kwargs):
            deps.evidence.read("example.py")
            return SimpleNamespace(output="fixture streamed answer [S1]")
        run = AsyncMock(side_effect=respond)
        async def collect():
            return "".join([part async for part in qa_agent.ask_stream(
                "question", URL, snapshot.root, collection_name="fixed-collection", commit_sha=self.first,
            )])
        with patch.object(qa_agent, "_get_agent", return_value=SimpleNamespace(run=run)), patch.object(qa_agent, "evaluate", return_value=1), patch.object(qa_agent, "save_qa") as save:
            self.assertIn(f"/blob/{self.first}/example.py#L1-L2", asyncio.run(collect()))
            self.assertEqual(run.call_args.kwargs["deps"].commit_sha, self.first)
            self.assertEqual(save.call_args.kwargs["commit_sha"], self.first)


class FetchVersionTests(unittest.TestCase):
    def test_refresh_fetches_default_head_without_resetting_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = Repo.init(root / "remote.git", bare=True)
            work = Repo.init(root / "work")
            clone = None
            try:
                file = root / "work" / "value.py"
                file.write_text("value = 1\n")
                work.index.add(["value.py"])
                first = work.index.commit("first", author=ACTOR, committer=ACTOR).hexsha
                work.create_remote("origin", remote.working_dir)
                work.git.push("origin", "HEAD:refs/heads/main")
                remote.git.symbolic_ref("HEAD", "refs/heads/main")
                clone = Repo.clone_from((root / "remote.git").as_uri(), root / "clone", depth=1)
                file.write_text("value = 2\n")
                work.index.add(["value.py"])
                second = work.index.commit("second", author=ACTOR, committer=ACTOR).hexsha
                work.git.push("origin", "HEAD:refs/heads/main")
                self.assertEqual(service._fetch_default_commit(str(root / "clone")), second)
                self.assertEqual(clone.head.commit.hexsha, first)
            finally:
                if clone:
                    clone.close()
                work.close()
                remote.close()


if __name__ == "__main__":
    unittest.main()
