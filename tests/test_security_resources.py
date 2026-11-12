import asyncio
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from git import Actor, Repo
import httpx
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent import qa_agent
from agent.runtime import RunPolicy
from security import resources as limits, git_transport as transport
from security.sources import secure_chunk_repo
from indexer import snapshots, cloner
from tests import test_runtime as fixtures


class TransportBoundaryTests(unittest.TestCase):
    def test_clone_removal_rejects_noncanonical_input_and_symlinks(self):
        with TemporaryDirectory() as directory, patch.object(cloner, "CLONE_DIR", directory):
            keep = Path(directory)/"keep"
            keep.mkdir()
            (keep/"data").write_text("KEEP")
            with self.assertRaises(ValueError):
                cloner.remove_repo("https://example.invalid/../../keep")
            (Path(directory)/"example__repo").symlink_to(keep, target_is_directory=True)
            with self.assertRaises(ValueError):
                cloner.remove_repo("https://github.com/example/repo")
            self.assertEqual((keep/"data").read_text(), "KEEP")

    def test_anonymous_repository_check_refuses_private_redirected_or_wrong_identity(self):
        cases = [(200, {"private": False, "full_name": "example/repo"}, True),
                 (200, {"private": True, "full_name": "example/repo"}, False),
                 (200, {"private": False, "full_name": "other/repo"}, False), (302, {}, False), (401, {}, False)]
        for status, body, passed in cases:
            requests = []
            def response(request):
                requests.append(request)
                self.assertNotIn("Authorization", request.headers)
                return httpx.Response(status, json=body, headers={"Location": "https://example.invalid/"})
            client = httpx.Client(transport=httpx.MockTransport(response))
            with self.subTest(status=status, body=body), patch.object(transport.httpx, "Client", return_value=client) as factory:
                if passed:
                    transport.require_public_repository("https://github.com/example/repo")
                else:
                    with self.assertRaises(ValueError):
                        transport.require_public_repository("https://github.com/example/repo")
                self.assertEqual(len(requests), 1)
                self.assertFalse(factory.call_args.kwargs["trust_env"])
                self.assertFalse(factory.call_args.kwargs["follow_redirects"])

    def test_clone_is_atomic_without_checkout_and_failed_attempt_leaves_other_data(self):
        with TemporaryDirectory() as directory, patch.object(cloner, "CLONE_DIR", directory), patch.object(cloner, "require_public_repository"):
            stable = Path(directory)/"unrelated"
            stable.mkdir()
            (stable/"keep").write_text("KEEP")
            def failed(arguments, **kwargs):
                self.assertIn("--no-checkout", arguments)
                Path(arguments[-1]).mkdir()
                raise ValueError("FIXTURE_CLONE_FAILURE")
            with patch.object(cloner, "run_git", side_effect=failed), self.assertRaisesRegex(ValueError, "FIXTURE_CLONE_FAILURE"):
                cloner.clone_repo("https://github.com/example/repo")
            self.assertEqual((stable/"keep").read_text(), "KEEP")
            self.assertFalse((Path(directory)/"example__repo").exists())
            self.assertFalse(list(Path(directory).glob(".clone-*")))

    def test_git_timeout_kills_only_the_spawned_group_and_does_not_inherit_model_keys(self):
        original = subprocess.Popen
        children, commands = [], []
        def spawn(command, **kwargs):
            commands.append(command)
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            child = original([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
            children.append(child)
            return child
        with TemporaryDirectory() as directory, patch.object(transport.subprocess, "Popen", side_effect=spawn), \
             patch.object(limits, "MAX_GIT_SECONDS", .05), patch.dict(os.environ, {"OPENAI_API_KEY": "LOCAL_ONLY_CANARY"}):
            with self.assertRaisesRegex(ValueError, "时间上限"):
                transport.run_git(["--version"], cwd=directory, size_path=directory)
        self.assertIsNotNone(children[0].poll())
        self.assertIn("protocol.allow=never", commands[0])
        self.assertIn("http.followRedirects=false", commands[0])
        self.assertIn("core.hooksPath=/dev/null", commands[0])

    def test_completed_git_process_is_still_checked_against_disk_budget(self):
        original = subprocess.Popen
        with TemporaryDirectory() as directory:
            def spawn(command, **kwargs):
                code = f"from pathlib import Path; Path({str(Path(directory)/'large')!r}).write_bytes(b'x'*100)"
                return original([sys.executable, "-c", code], **kwargs)
            with patch.object(transport.subprocess, "Popen", side_effect=spawn), patch.object(limits, "MAX_GIT_BYTES", 10):
                with self.assertRaisesRegex(ValueError, "磁盘"):
                    transport.run_git(["--version"], cwd=directory, size_path=directory)


class SnapshotResourceTests(unittest.TestCase):
    def test_oversized_file_is_catalogued_but_never_materialized_or_parsed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with Repo.init(root/"work") as repo:
                (root/"work/small.py").write_text("VALUE = 1\n")
                (root/"work/large.py").write_bytes(b"x"*(limits.MAX_SOURCE_BYTES+1))
                repo.index.add(["small.py", "large.py"])
                actor = Actor("Fixture", "fixture@example.invalid")
                repo.index.commit("size fixture", author=actor, committer=actor)
                snapshot = snapshots.create_snapshot("https://github.com/example/resource", str(root/"work"), storage_dir=root/"snapshots")
                self.assertFalse(snapshot.files["large.py"]["materialized"])
                self.assertFalse((Path(snapshot.root)/"large.py").exists())
                with self.assertRaises(ValueError):
                    snapshot.read("large.py")
                self.assertEqual({chunk.file_path for chunk in secure_chunk_repo(snapshot)}, {"small.py"})

    def test_snapshot_budget_failure_does_not_publish_or_leave_staging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with Repo.init(root/"work") as repo:
                (root/"work/a.py").write_text("a = 1\n")
                (root/"work/b.py").write_text("b = 2\n")
                repo.index.add(["a.py", "b.py"])
                actor = Actor("Fixture", "fixture@example.invalid")
                repo.index.commit("budget fixture", author=actor, committer=actor)
                with patch.object(limits, "MAX_SNAPSHOT_BYTES", 8), self.assertRaisesRegex(ValueError, "总大小"):
                    snapshots.create_snapshot("https://github.com/example/resource", str(root/"work"), storage_dir=root/"snapshots")
                self.assertFalse(list((root/"snapshots").rglob("manifest.json")))
                self.assertFalse(list((root/"snapshots").rglob(".building-*")))


class AgentResourceTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.RuntimeTurnTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def test_custom_policy_cannot_raise_server_caps(self):
        for policy in (RunPolicy(request_limit=51), RunPolicy(tool_calls_limit=51), RunPolicy(timeout_seconds=301.0)):
            with self.subTest(policy=policy), patch.object(qa_agent, "_get_agent", side_effect=AssertionError("NO_MODEL")):
                with self.assertRaisesRegex(ValueError, "安全上限"):
                    asyncio.run(qa_agent._run_answer("local", self.helper.deps(source_only=True), policy=policy))

    def test_byte_limit_applies_before_model_and_default_parallel_tools_are_bounded(self):
        with patch.object(qa_agent, "_get_agent", side_effect=AssertionError("NO_MODEL")), self.assertRaisesRegex(ValueError, "字节"):
            asyncio.run(qa_agent._run_answer("x"*(limits.MAX_SOURCE_BYTES+1), self.helper.deps(source_only=True)))
        def respond(messages, info):
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "alpha.py"}) for _ in range(51)])
        with self.assertRaises(UsageLimitExceeded):
            self.helper.run_turn(respond)
        self.assertEqual(self.helper.base.load().revision, 0)
