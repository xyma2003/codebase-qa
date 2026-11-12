import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

from git import Actor
from pydantic_ai import models

from agent import qa_agent
from agent.conversations import load_conversation
from evaluation import archive, runner
from evaluation.archive import pack_archive, verify_archive
from evaluation.local_model import LocalModel
from tests import test_evaluation as fixtures

ROOT = Path(__file__).resolve().parents[1]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        # Reuse the small four-category data fixture without inheriting its tests.
        self.fixture = fixtures.EvaluationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        source = f.work / "sample.py"
        source.write_text(source.read_text() + "def demo():\n    return VALUE\n")
        f.repo.index.add(["sample.py"])
        actor = Actor("Fixture", "fixture@example.invalid")
        commit = f.repo.index.commit("local runner source", author=actor, committer=actor)
        f.manifest["sources"][0].update(commit_sha=commit.hexsha, tree_sha=commit.tree.hexsha)
        f.evidence[0]["file_sha256"] = archive.digest(source.read_bytes())
        f.cases[2]["turns"][0]["mode"] = "plan"
        f.gold[0]["turns"][0]["checkpoints"].append("GOLD_ONLY_TRIPWIRE_NOT_A_USER_INPUT")
        # Give each turn a unique question so failure injection is unambiguous.
        for case in f.cases:
            for turn in case["turns"]:
                turn["question"] = f"fixture {case['case_id']} {turn['turn_id']}"
        f.save()
        self.run_dir = f.root / "run"

    def run_fixture(self, **kwargs):
        return asyncio.run(runner.run_local(self.fixture.data, self.fixture.work, self.run_dir, **kwargs))

    def read(self, path):
        return archive.read_json((self.run_dir / path).read_bytes())

    def reseal(self):
        (self.run_dir / "archive.json").unlink()
        archive.seal(self.run_dir)

    def edit(self, relative, update):
        path = self.run_dir / relative
        data = self.read(relative)
        update(data)
        path.write_text(json.dumps(data, ensure_ascii=False))
        self.reseal()

    def test_complete_run_uses_real_framework_tools_and_has_no_quality_scores(self):
        with patch.object(qa_agent, "save_qa") as cache:
            result = self.run_fixture()
            cache.assert_not_called()
        self.assertEqual((result["completed_turns"], result["required_turns"]), (5, 5))
        self.assertIsNone(result["quality_score"])
        self.assertEqual(self.read("summary.json")["reviewed_turns"], 0)
        self.assertIsNone(self.read("summary.json")["pass_rate"])
        self.assertTrue(self.read("turns/003-01/output.json")["plan"])
        self.assertFalse((self.fixture.root / "executed").exists())

    def test_safety_caps_are_archived_and_cannot_be_relabelled(self):
        from security.resources import default_run_limits
        self.run_fixture()
        self.assertEqual(self.read("run.json")["server_safety_limits"], default_run_limits())
        self.assertEqual(self.read("turns/001-01/output.json")["runtime"]["enforced_limits"], default_run_limits())
        self.edit("run.json", lambda data: data["server_safety_limits"].update(request_limit=999))
        with self.assertRaisesRegex(ValueError, "safety limits"):
            verify_archive(self.run_dir, self.fixture.work)

    def test_inputs_and_captured_requests_exclude_gold_and_other_cases(self):
        self.run_fixture()
        for folder in (self.run_dir / "turns").iterdir():
            self.assertNotIn("GOLD_ONLY_TRIPWIRE", (folder / "requests.json").read_text())
            self.assertNotIn("GOLD_ONLY_TRIPWIRE", (folder / "input.json").read_text())
        requests = self.read("turns/001-02/requests.json")
        contents = [part["content"] for message in requests[0]["messages"] for part in message["parts"] if part["part_kind"] == "user-prompt"]
        self.assertEqual(contents, ["fixture C1 T1", "fixture C1 T2"])
        self.assertEqual(self.read("turns/001-01/input.json")["conversation_id"], self.read("turns/001-02/input.json")["conversation_id"])
        self.assertNotEqual(self.read("turns/001-01/input.json")["conversation_id"], self.read("turns/002-01/input.json")["conversation_id"])

    def test_failure_keeps_trace_blocks_dependent_turn_and_stops_other_cases(self):
        backend = LocalModel(fail_question="fixture C1 T1")
        result = self.run_fixture(backend=backend)
        self.assertEqual((result["failed_turns"], result["blocked_turns"], result["not_run_turns"]), (1, 1, 3))
        self.assertEqual(result["attempted_turns"], 1)
        self.assertEqual(len(backend.requests), 2)  # One actual tool result, then error; no retry.
        error = self.read("turns/001-01/error.json")
        self.assertIn("LOCAL_FIXTURE_FAILURE_AFTER_TOOL", error["traceback"])
        self.assertTrue(self.read("turns/001-01/trace.json")["returns"])
        self.assertFalse((self.run_dir / "turns/001-01/output.json").exists())
        source = self.fixture.manifest["sources"][0]
        request = self.read("turns/001-01/input.json")
        state = load_conversation(request["conversation_id"], source["repo_url"], source["commit_sha"], request["scope_token"],
                                  storage_dir=self.run_dir / "_state/conversations")
        self.assertEqual(state.revision, 0)
        self.assertIsNone(self.read("summary.json")["pass_rate"])

    def test_timeout_is_archived_without_retry_and_restores_global_model_guard(self):
        backend = LocalModel()
        original = backend.respond
        async def slow(messages, info):
            response = await original(messages, info)
            await asyncio.sleep(0.1)
            return response
        backend.respond = slow
        allowed = models.ALLOW_MODEL_REQUESTS
        result = self.run_fixture(backend=backend, turn_timeout=0.01)
        self.assertEqual(result["failed_turns"], 1)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(self.read("turns/001-01/error.json")["kind"], "TimeoutError")
        self.assertEqual(models.ALLOW_MODEL_REQUESTS, allowed)

    def test_completed_and_failed_directories_cannot_be_overwritten(self):
        self.run_fixture(backend=LocalModel(fail_question="fixture C1 T1"))
        before = (self.run_dir / "archive.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_fixture()
        self.assertEqual((self.run_dir / "archive.json").read_bytes(), before)

    def test_changed_output_is_rejected_even_if_only_one_byte_changes(self):
        self.run_fixture()
        path = self.run_dir / "turns/001-01/output.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify_archive(self.run_dir, self.fixture.work)

    def test_review_output_binding_is_checked_beyond_the_inventory(self):
        self.run_fixture()
        path = self.run_dir / "reviews.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["output_sha256"] = "a" * 64
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.reseal()
        with self.assertRaisesRegex(ValueError, "wrong output"):
            verify_archive(self.run_dir, self.fixture.work)

    def test_resealed_runtime_usage_is_checked_beyond_output_binding(self):
        self.run_fixture()
        relative = "turns/001-01/output.json"
        data = self.read(relative)
        data["runtime"]["requests"] += 1
        output = self.run_dir / relative
        output.write_text(json.dumps(data))
        path = self.run_dir / "reviews.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["output_sha256"] = archive.digest(output.read_bytes())
        path.write_text("\n".join(json.dumps(row) for row in rows)+"\n")
        self.reseal()
        with self.assertRaisesRegex(ValueError, "runtime record"):
            verify_archive(self.run_dir, self.fixture.work)

    def test_resealed_input_context_and_source_tampering_are_rejected(self):
        self.run_fixture()
        self.edit("turns/001-01/input.json", lambda data: data["turn"].update(question="changed"))
        with self.assertRaisesRegex(ValueError, "input/source"):
            verify_archive(self.run_dir, self.fixture.work)

    def test_resealed_native_response_and_trace_tampering_are_rejected(self):
        self.run_fixture()
        path = self.run_dir / "turns/001-01/native_output.json"
        path.write_text(json.dumps("not the model response"))
        self.reseal()
        with self.assertRaisesRegex(ValueError, "actual model response"):
            verify_archive(self.run_dir, self.fixture.work)

    def test_resealed_citation_wrong_version_is_rejected(self):
        self.run_fixture()
        self.edit("turns/001-01/output.json", lambda data: data["citations"][0].update(commit_sha="a" * 40))
        path = self.run_dir / "reviews.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["output_sha256"] = archive.digest((self.run_dir / "turns/001-01/output.json").read_bytes())
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.reseal()
        with self.assertRaisesRegex(ValueError, "another source"):
            verify_archive(self.run_dir, self.fixture.work)

    def test_source_only_context_blocks_vector_and_memory_tools(self):
        from agent.tools import RepoDeps
        from indexer import vector_store
        from memory import knowledge
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
        from pydantic_ai.models.function import FunctionModel
        async def model(messages, info):
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=[ToolCallPart("search_code", {"query": "demo"}),
                                            ToolCallPart("list_repo_files", {})])
            returned = [part.content for part in messages[-1].parts if isinstance(part, ToolReturnPart)]
            self.assertTrue(all("source-only" in value for value in returned))
            return ModelResponse(parts=[TextPart("local guard fixture")])
        deps = RepoDeps("https://github.com/example/eval-fixture", str(self.fixture.work), "placeholder",
                        self.fixture.repo.head.commit.hexsha, source_only=True)
        agent = qa_agent.build_agent(FunctionModel(model))
        with patch.object(vector_store, "_get_client") as vectors, patch.object(knowledge, "_get_client") as memory:
            asyncio.run(agent.run("local guard", deps=deps))
            vectors.assert_not_called()
            memory.assert_not_called()

    def test_resealed_progress_order_and_real_model_claim_are_rejected(self):
        self.run_fixture()
        progress = self.run_dir / "progress.jsonl"
        original = progress.read_bytes()
        rows = original.decode().splitlines()
        progress.write_text("\n".join(reversed(rows)) + "\n")
        self.reseal()
        with self.assertRaisesRegex(ValueError, "journal"):
            verify_archive(self.run_dir, self.fixture.work)
        progress.write_bytes(original)
        self.reseal()
        self.edit("run.json", lambda data: data.update(real_model_called=True))
        with self.assertRaisesRegex(ValueError, "local evaluation"):
            verify_archive(self.run_dir, self.fixture.work)

    def test_packaging_refuses_overwrite_and_an_output_inside_the_archive(self):
        self.run_fixture()
        destination = self.fixture.root / "run.zip"
        pack_archive(self.run_dir, destination)
        with self.assertRaises(FileExistsError):
            pack_archive(self.run_dir, destination)
        with self.assertRaisesRegex(ValueError, "external destination"):
            pack_archive(self.run_dir, self.run_dir / "nested.zip")

    def test_archive_round_trip_in_new_process_needs_no_writable_state(self):
        expected = self.run_fixture()
        package = self.fixture.root / "run.zip"
        pack_archive(self.run_dir, package)
        extracted = self.fixture.root / "extracted"
        with zipfile.ZipFile(package) as zipped:
            self.assertFalse(any(name.startswith("_state/") for name in zipped.namelist()))
            zipped.extractall(extracted)
        completed = subprocess.run([sys.executable, str(ROOT / "scripts/verify_evaluation_archive.py"),
                                    str(extracted), "--repository", str(self.fixture.work)],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), expected)

    def test_missing_marker_added_file_and_symlink_are_not_valid_archives(self):
        self.run_fixture()
        marker = self.run_dir / "archive.json"
        content = marker.read_bytes()
        marker.unlink()
        with self.assertRaises(FileNotFoundError):
            verify_archive(self.run_dir, self.fixture.work)
        marker.write_bytes(content)
        extra = self.run_dir / "untracked.json"
        extra.write_text("{}")
        with self.assertRaisesRegex(ValueError, "inventory"):
            verify_archive(self.run_dir, self.fixture.work)
        extra.unlink()
        (self.run_dir / "link.json").symlink_to(self.run_dir / "run.json")
        with self.assertRaisesRegex(ValueError, "symbolic"):
            verify_archive(self.run_dir, self.fixture.work)

    def test_disk_write_failure_leaves_incomplete_run_and_no_final_marker(self):
        original = runner.write_json
        def fail_output(path, value):
            if Path(path).name == "output.json":
                raise OSError("LOCAL_ARCHIVE_DISK_FAILURE")
            return original(path, value)
        with patch.object(runner, "write_json", side_effect=fail_output):
            with self.assertRaisesRegex(OSError, "LOCAL_ARCHIVE_DISK_FAILURE"):
                self.run_fixture()
        self.assertFalse((self.run_dir / "archive.json").exists())
        self.assertTrue((self.run_dir / "turns/001-01/input.json").exists())
        with self.assertRaises(FileNotFoundError):
            verify_archive(self.run_dir, self.fixture.work)

    def test_invalid_backend_timeout_or_dataset_does_not_start_a_run(self):
        for kwargs in ({"backend": object()}, {"turn_timeout": 0}, {"turn_timeout": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_fixture(**kwargs)
        self.assertFalse(self.run_dir.exists())
        self.fixture.cases[0]["expected_answer"] = "gold leakage"
        self.fixture.save()
        with self.assertRaises(ValueError):
            self.run_fixture()
        self.assertFalse(self.run_dir.exists())


if __name__ == "__main__":
    unittest.main()
