import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from git import Actor, Repo
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agent import qa_agent
from agent.tools import RepoDeps
from indexer import snapshots, vector_store
from indexer.chunker import CodeChunk, chunk_repo
from indexer.code_search import find_in_snapshot
from indexer.source_reader import SourceReadError, read_source_file
from security.sources import MAX_SOURCE_BYTES, SourcePolicyError, ensure_safe_text, safe_chunks, sensitive_path


class SourcePolicyTests(unittest.TestCase):
    def test_sensitive_paths_and_safe_templates(self):
        for path in (".env", "src/.ENV.production", ".ssh/id_ed25519", ".aws/config", "credentials.json", "secrets.yaml", "server.key", "a\u202e.py"):
            with self.subTest(path=path):
                self.assertTrue(sensitive_path(path))
        for path in (".env.example", "settings/env.py", "docs/security.md", "config.py", "src/中文.py"):
            with self.subTest(path=path):
                self.assertFalse(sensitive_path(path))

    def test_content_tokens_private_keys_and_configured_credentials_are_blocked(self):
        values = ["sk-"+"A"*32, "ghp_"+"B"*36, "AKIA"+"A"*16,
                  "-----BEGIN "+"PRIVATE KEY-----", 'password="long-test-password"', 'password="UPPER_CASE_PASSWORD"']
        for value in values:
            with self.subTest(kind=value[:6]), self.assertRaises(SourcePolicyError) as error:
                ensure_safe_text(value)
            self.assertNotIn(value, str(error.exception))
        with patch("config.OPENAI_API_KEY", "runtime-configured-canary"):
            with self.assertRaises(SourcePolicyError):
                ensure_safe_text("prefix runtime-configured-canary suffix")
        for safe in ('OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")', 'api_key=OPENAI_API_KEY',
                     'password=password', 'api_key=current_token', 'OPENAI_API_KEY=your-siliconflow-key', 'sk-ant-'+'x'*20):
            ensure_safe_text(safe)
        import json
        ensure_safe_text(json.dumps({"text": "OPENAI_API_KEY=sk-ant-"+"x"*20+"\n"}))

    def test_vector_write_and_query_checks_run_before_storage_or_embedding(self):
        unsafe = CodeChunk("test", ".env", 1, 1, "other", "test")
        with patch.object(vector_store, "_get_client", side_effect=AssertionError("STORAGE_MUST_NOT_RUN")):
            with self.assertRaises(SourcePolicyError):
                vector_store.build_collection([unsafe], "url", "collection", "a"*40)
            with self.assertRaises(SourcePolicyError):
                vector_store.index_chunks([unsafe], "url")
            with self.assertRaises(SourcePolicyError):
                vector_store.search("sk-"+"A"*32, "url")

    def test_legacy_vectors_cannot_return_credentials_or_sensitive_files(self):
        for path, content in (("config.py", "sk-"+"A"*32), ("credentials.json", "value")):
            collection = SimpleNamespace(query=lambda **kwargs: {"documents": [[content]], "metadatas": [[{
                "file_path": path, "start_line": 1, "end_line": 1, "chunk_type": "other", "name": path}]]})
            with patch.object(vector_store, "_get_client", return_value=SimpleNamespace(get_collection=lambda **kwargs: collection)), \
                 patch.object(vector_store, "_get_embed_fn", return_value=None), self.assertRaises(SourcePolicyError):
                vector_store.search("config", "https://github.com/example/fixture")


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / "work"
        self.repo = Repo.init(self.work)
        self.addCleanup(self.repo.close)
        self.url = "https://github.com/example/security-fixture"
        self.secret = "sk-"+"A"*32
        contents = {"sample.py": "def value():\n    return 1\n", ".env": "OPENAI_API_KEY="+self.secret,
                    "credentials.json": '{"value":"'+self.secret+'"}', "leaky.py": 'value = "'+self.secret+'"',
                    ".env.example": "OPENAI_API_KEY=your-siliconflow-key\n"}
        for name, data in contents.items():
            (self.work/name).write_text(data)
        self.repo.index.add(list(contents))
        actor = Actor("Fixture", "fixture@example.invalid")
        self.repo.index.commit("security fixture", author=actor, committer=actor)
        self.snapshot = snapshots.create_snapshot(self.url, str(self.work), storage_dir=self.root/"snapshots")

    def test_new_snapshot_excludes_sensitive_files_before_materialization(self):
        for name in (".env", "credentials.json", "leaky.py"):
            self.assertNotIn(name, self.snapshot.files)
            self.assertIn(name, self.snapshot.skipped)
            self.assertFalse((Path(self.snapshot.root)/name).exists())
            with self.assertRaises(SourceReadError):
                self.snapshot.read(name)
        self.assertIn("your-siliconflow-key", self.snapshot.read(".env.example").content)
        self.assertEqual(len(safe_chunks(chunk_repo(self.snapshot.root), self.snapshot)), 1)

    def test_legacy_snapshot_and_lookup_cannot_bypass_filename_policy(self):
        import hashlib, json
        manifest = Path(self.snapshot.root).parent/"manifest.json"
        data = json.loads(manifest.read_text())
        value = b'LEGACY_SENSITIVE_CANARY'
        (Path(self.snapshot.root)/"credentials.json").write_bytes(value)
        data["files"]["credentials.json"] = {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
        manifest.write_text(json.dumps(data))
        legacy = snapshots.load_snapshot(self.snapshot.root, verify=True)
        self.assertNotIn("credentials.json", legacy.files)
        with self.assertRaises(SourceReadError):
            legacy.read("credentials.json")
        found = find_in_snapshot(legacy.root, self.url, legacy.commit_sha, "credentials", mode="path")
        self.assertFalse(found.hits)

    def test_whole_source_read_is_bounded_and_unknown_file_content_is_checked(self):
        path = self.work/"huge.txt"
        path.write_bytes(b"a"*(MAX_SOURCE_BYTES+1))
        with self.assertRaisesRegex(SourceReadError, "byte limit"):
            read_source_file(str(self.work), "huge.txt", 1, 1)
        with self.assertRaises(SourceReadError):
            read_source_file(str(self.work), "leaky.py")

    def test_adversarial_model_tool_call_cannot_read_committed_env(self):
        calls = []
        def respond(messages, info):
            returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            if not returns:
                calls.append(1)
                return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": ".env"})])
            self.assertNotIn(self.secret, returns[-1].content)
            self.assertIn("敏感文件", returns[-1].content)
            return ModelResponse(parts=[TextPart("本地攻击模拟：敏感文件读取被程序拒绝。")])
        deps = RepoDeps(self.url, self.snapshot.root, "fixture", self.snapshot.commit_sha, source_only=True)
        answer = asyncio.run(qa_agent._run_answer("本地攻击模拟", deps,
            agent=qa_agent.build_agent(FunctionModel(respond))))
        self.assertEqual(len(calls), 1)
        self.assertNotIn(self.secret, answer.model_dump_json())
