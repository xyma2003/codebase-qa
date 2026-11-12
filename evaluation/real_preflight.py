"""Read-only gates for real evaluations; no network requests or configuration edits."""

import importlib.metadata
import json
from pathlib import Path

from git import Repo
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, UserPromptPart, TextPart

from evaluation.dataset import digest, read_json

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "7ed1f8f22e3c1caf1c23b9ca976bf9e86a5feffb"
BASE_FILES = ("inspect.json", "index.json", "ask.json", "answer.txt", "model-messages.json", "requirements-resolved.txt")


def check_baseline(directory, *, source_repository=ROOT):
    directory = Path(directory)
    payloads = {f"{phase}.json": (directory / f"{phase}.json").read_bytes() for phase in ("inspect", "index", "ask")}
    records = {phase: read_json(payloads[f"{phase}.json"]) for phase in ("inspect", "index", "ask")}
    if any(record.get("phase") != phase or record.get("status") != "passed" for phase, record in records.items()):
        raise ValueError("先完成原版真实问答，提供 inspect、index、ask 均通过的成功基线")
    payloads.update({name: (directory / name).read_bytes() for name in BASE_FILES if name not in payloads})
    inspected, indexed, asked = (records[phase]["result"] for phase in ("inspect", "index", "ask"))
    if inspected["source_sha"] != BASE_SHA or inspected["source_matches_baseline"] is not True or indexed["target_sha"] != BASE_SHA:
        raise ValueError("成功基线必须来自原版代码和原版索引")
    with Repo(source_repository) as repo:
        expected = {item.path: digest(item.data_stream.read()) for item in repo.commit(BASE_SHA).tree.traverse()
                    if item.type == "blob" and (item.path.endswith(".py") or item.path in ("requirements.txt", ".streamlit/config.toml"))}
    if inspected["source_sha256"] != expected:
        raise ValueError("原版应用源码摘要不匹配")
    configuration = asked["effective_configuration"]
    if configuration != inspected["effective_configuration"]:
        raise ValueError("基线检查和问答使用了不同配置")
    if (configuration["LLM_PROVIDER"] != "openai" or configuration["OPENAI_MODEL"] != "Qwen/Qwen3-32B"
            or configuration["OPENAI_API_BASE"].rstrip("/") != "https://api.siliconflow.cn/v1"
            or configuration["credentials_present"]["OPENAI_API_KEY"] is not True):
        raise ValueError("基线不是已选定的 SiliconFlow Qwen/Qwen3-32B 成功配置")
    if (indexed["chunk_line_checks"] != "passed" or indexed["persisted_content_readback"] != "passed"
            or indexed["indexed_chunk_count"] != indexed["chunk_count"] or indexed["retrieval_count"] < 1):
        raise ValueError("原版索引尚未通过回读和检索检查")
    messages = ModelMessagesTypeAdapter.validate_json(payloads["model-messages.json"])
    responses = [message for message in messages if isinstance(message, ModelResponse)]
    answer = payloads["answer.txt"].decode().removesuffix("\n")
    prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
    if (prompts != [asked["question"]] or asked["question"] != inspected["question"]
            or asked["original_entrypoint"] != "agent.qa_agent.ask_stream" or asked["answer_chars"] < 1
            or len(answer) != asked["answer_chars"] or not responses
            or answer != "".join(part.content for part in responses[-1].parts if isinstance(part, TextPart))):
        raise ValueError("成功基线缺少完整原生消息或非空回答")
    packages = payloads["requirements-resolved.txt"].decode().splitlines()
    if len(packages) != inspected["package_count"] or len(set(packages)) != len(packages):
        raise ValueError("基线依赖记录不完整")
    inventory = {name: digest(value) for name, value in payloads.items()}
    return {"format_version": 1, "sha256": inventory,
            "fingerprint": digest(json.dumps(inventory, sort_keys=True).encode()),
            "configuration": configuration, "packages": sorted(packages),
            "baseline_answer_quality": asked["answer_quality"]}


def check_runtime(baseline, *, fixture=False):
    from scripts.run_baseline import configuration
    current = configuration()
    expected = baseline["configuration"]
    if not fixture and not current["credentials_present"]["OPENAI_API_KEY"]:
        raise ValueError("SiliconFlow 密钥未配置，未发送模型请求")
    ignored = {"credentials_present"}
    if fixture:
        ignored |= {"LLM_PROVIDER", "LLM_MODEL_STRING"}
    changes = {name: {"baseline": value, "current": current.get(name)} for name, value in expected.items()
               if name not in ignored and current.get(name) != value}
    packages = sorted(f"{item.metadata['Name']}=={item.version}" for item in importlib.metadata.distributions() if item.metadata.get("Name"))
    if changes or packages != baseline["packages"]:
        difference = {"settings": changes, "packages_added": sorted(set(packages) - set(baseline["packages"])),
                      "packages_removed": sorted(set(baseline["packages"]) - set(packages))}
        raise ValueError("当前配置与成功基线不一致，不自动改参数：" + json.dumps(difference, ensure_ascii=False))
    return current
