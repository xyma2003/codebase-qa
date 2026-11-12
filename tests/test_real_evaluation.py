"""Real SDK + in-memory API transport + real temporary vector storage."""

import asyncio
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import chromadb
from chromadb.api.types import EmbeddingFunction
import httpx
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from evaluation.archive import verify_archive, pack_archive, seal
from evaluation.dataset import digest, read_json
from evaluation.indexes import build_indexes
from evaluation.real_preflight import BASE_FILES, check_baseline
from evaluation.real_runner import run_real
from evaluation.reviews import prepare_review
from evaluation.reporting import import_review, verify_report
from indexer import vector_store
from tests import test_evaluation_runner as fixtures

ROOT = Path(__file__).resolve().parents[1]


class FixtureEmbedding(EmbeddingFunction):
    def __init__(self):
        pass
    def __call__(self, input):
        return [[1.0, 0.0, 0.5] for _ in input]

    @staticmethod
    def name():
        return "fixture-embedding-no-network"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return FixtureEmbedding()


def baseline_fixture(destination):
    """Construct test-only gate inputs; the resulting run remains provider_fixture."""
    original = ROOT / "docs/baseline/runs/20261110-stage0"
    destination.mkdir()
    for name in ("inspect.json", "index.json", "requirements-resolved.txt"):
        shutil.copyfile(original / name, destination / name)
    inspected = read_json((destination / "inspect.json").read_bytes())
    configuration = inspected["result"]["effective_configuration"]
    configuration.update(LLM_PROVIDER="openai", LLM_MODEL_STRING="openai:Qwen/Qwen3-32B")
    configuration["credentials_present"]["OPENAI_API_KEY"] = True
    packages = sorted(f"{item.metadata['Name']}=={item.version}" for item in importlib.metadata.distributions() if item.metadata.get("Name"))
    (destination / "requirements-resolved.txt").write_text("\n".join(packages) + "\n")
    inspected["result"]["package_count"] = len(packages)
    (destination / "inspect.json").write_text(json.dumps(inspected))
    answer = "Synthetic baseline gate fixture; no live model call."
    question = inspected["result"]["question"]
    (destination / "ask.json").write_text(json.dumps({"phase": "ask", "status": "passed", "result": {
        "effective_configuration": configuration, "question": question, "answer_chars": len(answer),
        "original_entrypoint": "agent.qa_agent.ask_stream", "answer_quality": "requires_review"}}))
    (destination / "answer.txt").write_text(answer + "\n")
    (destination / "model-messages.json").write_bytes(ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[UserPromptPart(question)]), ModelResponse(parts=[TextPart(answer)])]))


class ProviderFixture:
    def __init__(self, *, fail=False):
        self.payloads = []
        self.fail = fail

    def respond(self, request):
        body = json.loads(request.content)
        self.payloads.append(body)
        if self.fail:
            return httpx.Response(400, json={"error": {"message": "ORIGINAL_FIXTURE_FAILURE FIXTURE_SECRET_NOT_REAL", "type": "invalid_request_error"}})
        messages = body["messages"]
        latest = max(number for number, item in enumerate(messages) if item["role"] == "user")
        returns = [item for item in messages[latest:] if item["role"] == "tool"]
        if not returns:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"search-{len(self.payloads)}", "type": "function",
                "function": {"name": "search_code", "arguments": json.dumps({"query": "demo"})}}]}
        else:
            reference = re.search(r"\[(S\d+)\]", returns[-1]["content"])[1]
            file_path = re.search(r"\*\*(.+?)\*\* \(lines", returns[-1]["content"])[1]
            if any(tool["function"]["name"] == "submit_change_plan" for tool in body["tools"]):
                proposal = {"goal": "接口模拟规划，不执行修改", "current_behavior": [{"description": "测试源码", "references": [reference]}],
                    "file_changes": [{"file_path": file_path, "action": "modify", "reason": "模拟接入", "changes": ["测试建议"], "references": [reference]}],
                    "steps": [{"description": "模拟步骤", "file_paths": [file_path]}],
                    "validation": [{"method": "核对归档", "expected_result": "记录完整", "command": None}], "open_questions": []}
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"plan-{len(self.payloads)}", "type": "function",
                    "function": {"name": "submit_change_plan", "arguments": json.dumps(proposal)}}]}
            else:
                message = {"role": "assistant", "content": f"接口模拟，只验证调用、向量检索和归档 [{reference}]"}
        return httpx.Response(200, json={"id": f"fixture-{len(self.payloads)}", "object": "chat.completion", "created": 1,
            "model": "Qwen/Qwen3-32B", "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})

    async def run(self, dataset, repository, output, baseline, indexes):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.respond)) as client:
            model = OpenAIChatModel("Qwen/Qwen3-32B", provider=OpenAIProvider(api_key="FIXTURE_SECRET_NOT_REAL", base_url="https://api.siliconflow.cn/v1", http_client=client))
            return await run_real(dataset, repository, output, baseline_directory=baseline, index_directory=indexes, fixture_model=model)


class RealEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.RunnerTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.fixture = self.helper.fixture
        self.baseline = self.fixture.root / "baseline"
        baseline_fixture(self.baseline)
        self.indexes = self.fixture.root / "indexes"
        self.output = self.fixture.root / "provider-run"
        self.client = chromadb.PersistentClient(path=str(self.fixture.root / "vectors"))
        for context in (patch.object(vector_store, "_get_client", return_value=self.client),
                        patch.object(vector_store, "_get_embed_fn", return_value=FixtureEmbedding())):
            context.start()
            self.addCleanup(context.stop)
        build_indexes(self.fixture.data, self.fixture.work, self.indexes)

    def run_fixture(self, *, fail=False):
        self.provider = ProviderFixture(fail=fail)
        with patch("agent.tools.search_memory", side_effect=AssertionError("CACHE_MUST_NOT_RUN")), patch("agent.qa_agent.save_qa", side_effect=AssertionError("CACHE_MUST_NOT_RUN")):
            return asyncio.run(self.provider.run(self.fixture.data, self.fixture.work, self.output, self.baseline, self.indexes))

    def test_sdk_vector_tools_usage_history_plan_and_archive(self):
        receipt = self.run_fixture()
        self.assertEqual((receipt["backend"], receipt["real_model_called"], receipt["completed_turns"]), ("provider_fixture", False, 5))
        self.assertEqual(len(self.provider.payloads), 10)
        self.assertFalse((self.fixture.root / "executed").exists())
        for folder in (self.output / "turns").iterdir():
            metrics = read_json((folder / "metrics.json").read_bytes())
            self.assertEqual(len(metrics["provider_usage"]), 2)
            self.assertEqual(metrics["provider_usage"][0]["input_tokens"], 20)
            self.assertIsNone(metrics["cost"])
        self.assertTrue(read_json((self.output / "turns/003-01/output.json").read_bytes())["plan"])
        self.assertNotIn("GOLD_ONLY_TRIPWIRE", json.dumps(self.provider.payloads))
        requests = read_json((self.output / "turns/001-02/requests.json").read_bytes())
        self.assertEqual([part["content"] for message in requests[0]["messages"] for part in message["parts"] if part["part_kind"] == "user-prompt"], ["fixture C1 T1", "fixture C1 T2"])

    def test_original_failure_is_redacted_and_stops_without_rerun(self):
        receipt = self.run_fixture(fail=True)
        self.assertEqual((receipt["failed_turns"], receipt["blocked_turns"], receipt["not_run_turns"]), (1, 1, 3))
        self.assertEqual(len(self.provider.payloads), 1)
        error = (self.output / "turns/001-01/error.json").read_text()
        self.assertIn("ORIGINAL_FIXTURE_FAILURE", error)
        self.assertNotIn("FIXTURE_SECRET_NOT_REAL", error)
        self.assertIn("<redacted>", error)

    def test_missing_or_changed_baseline_and_index_refuse_before_requests(self):
        original = (self.baseline / "ask.json").read_bytes()
        record = read_json(original)
        record["status"] = "blocked"
        (self.baseline / "ask.json").write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "成功基线"):
            self.run_fixture()
        self.assertFalse(self.output.exists())
        (self.baseline / "ask.json").write_bytes(original)
        manifest = read_json((self.indexes / "index.json").read_bytes())
        manifest["sources"][0]["commit_sha"] = "a" * 40
        (self.indexes / "index.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "版本不一致"):
            self.run_fixture()
        self.assertFalse(self.output.exists())

    def test_real_mode_requires_key_without_building_or_calling_provider(self):
        with patch("config.OPENAI_API_KEY", ""), patch("evaluation.real_runner.OpenAIProvider") as provider:
            with self.assertRaisesRegex(ValueError, "密钥未配置"):
                asyncio.run(run_real(self.fixture.data, self.fixture.work, self.output, baseline_directory=self.baseline, index_directory=self.indexes))
            provider.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_blocked_original_record_and_source_hash_drift_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "成功基线"):
            check_baseline(ROOT / "docs/baseline/runs/20261110-stage0")
        path = self.baseline / "inspect.json"
        data = read_json(path.read_bytes())
        data["result"]["source_sha256"]["app.py"] = "0" * 64
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "源码摘要"):
            self.run_fixture()
        self.assertFalse(self.output.exists())

    def test_configuration_drift_is_reported_without_changing_it(self):
        with patch("config.MAX_CHUNK_TOKENS", 999):
            with self.assertRaisesRegex(ValueError, "MAX_CHUNK_TOKENS"):
                self.run_fixture()
            import config
            self.assertEqual(config.MAX_CHUNK_TOKENS, 999)
        self.assertFalse(self.output.exists())

    def test_changed_vector_contents_refuse_before_model_and_existing_outputs_never_overwrite(self):
        data = read_json((self.indexes / "index.json").read_bytes())
        collection = self.client.get_collection(data["sources"][0]["collection_name"], embedding_function=None)
        identity = collection.get()["ids"][0]
        collection.update(ids=[identity], documents=["altered source"])
        with self.assertRaisesRegex(ValueError, "向量存储"):
            self.run_fixture()
        self.assertFalse(self.output.exists())

    def test_production_model_cannot_be_injected_as_a_fixture(self):
        with self.assertRaisesRegex(ValueError, "MockTransport"):
            asyncio.run(run_real(self.fixture.data, self.fixture.work, self.output,
                baseline_directory=self.baseline, index_directory=self.indexes, fixture_model=object()))
        self.assertFalse(self.output.exists())

    def test_package_and_review_report_work_in_fresh_process(self):
        receipt = self.run_fixture()
        package = self.fixture.root / "provider.zip"
        pack_archive(self.output, package)
        extracted = self.fixture.root / "extracted"
        with zipfile.ZipFile(package) as zipped:
            zipped.extractall(extracted)
        result = subprocess.run([sys.executable, "scripts/verify_evaluation_archive.py", str(extracted), "--repository", str(self.fixture.work)], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_json(result.stdout), receipt)
        packet, report = self.fixture.root / "packet", self.fixture.root / "report"
        prepare_review(extracted, self.fixture.work, packet)
        outcome = import_review(extracted, self.fixture.work, packet, report)
        self.assertEqual(outcome["report_kind"], "human_review_of_provider_fixture")
        self.assertFalse(outcome["real_model_quality_evaluated"])
        self.assertEqual(verify_report(report, extracted, self.fixture.work), outcome)
        self.assertIn("接口模拟输出", (report / "report.md").read_text())

    def test_resealed_usage_backend_and_source_tampering_are_rejected(self):
        self.run_fixture()
        for name, update in (("turns/001-01/metrics.json", lambda row: row.update(provider_usage=[])),
                             ("run.json", lambda row: row.update(real_model_called=True)),
                             ("index-manifest.json", lambda row: row["sources"][0].update(commit_sha="a" * 40))):
            path = self.output / name
            original = path.read_bytes()
            data = read_json(original)
            update(data)
            path.write_text(json.dumps(data))
            (self.output / "archive.json").unlink()
            seal(self.output)
            with self.subTest(name=name), self.assertRaises(ValueError):
                verify_archive(self.output, self.fixture.work)
            path.write_bytes(original)
            (self.output / "archive.json").unlink()
            seal(self.output)


if __name__ == "__main__":
    unittest.main()
