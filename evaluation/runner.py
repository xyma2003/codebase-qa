"""Sequential, fail-fast local evaluation with immutable per-turn artifacts."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import time
import traceback

from git import Repo
from pydantic_ai import capture_run_messages, models
from pydantic_ai.messages import ModelMessagesTypeAdapter

from agent import qa_agent
from agent.conversations import create_conversation
from agent.tools import RepoDeps
from evaluation.archive import seal, tool_trace, verify_archive, write_json, write_rows
from evaluation.dataset import PAYLOADS, digest, validate_bundle
from evaluation.local_model import LocalModel
from evaluation.scoring import summarize_reviews
from indexer.snapshots import create_snapshot

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = (
    "security/sources.py",
    "agent/qa_agent.py", "agent/tools.py", "agent/evidence.py", "agent/planning.py", "agent/conversations.py", "agent/tracing.py", "agent/runtime.py",
    "indexer/snapshots.py", "indexer/source_reader.py", "indexer/code_search.py", "indexer/dependencies.py",
    "indexer/overview.py", "indexer/vector_store.py", "indexer/hybrid.py", "memory/quality.py", "memory/knowledge.py", "config.py", "requirements.txt",
    "evaluation/dataset.py", "evaluation/scoring.py", "evaluation/runner.py", "evaluation/archive.py", "evaluation/local_model.py",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _native(messages):
    return json.loads(ModelMessagesTypeAdapter.dump_json(messages))


async def run_local(dataset, repository, output, *, backend=None, turn_timeout=30):
    """Run the entire fixed denominator once. No remote backend, retry or resume."""
    if type(turn_timeout) not in {int, float} or not 0 < turn_timeout <= 300:
        raise ValueError("Local turn timeout must be between 0 and 300 seconds")
    backend = LocalModel() if backend is None else backend
    if type(backend) is not LocalModel:
        raise ValueError("Only the explicit local simulation backend is enabled")
    return await _run(dataset, repository, output, backend=backend, turn_timeout=turn_timeout)


async def _run(dataset, repository, output, *, backend, turn_timeout, indexed=None):
    bundle = validate_bundle(dataset, repository)
    inputs = bundle.model_inputs()
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "dataset").mkdir()
    for name in ("manifest.json", *PAYLOADS):
        shutil.copyfile(Path(dataset) / name, root / "dataset" / name)
    write_json(root / "inputs.json", inputs)
    with Repo(ROOT) as engine_repo:
        runtime_commit = engine_repo.head.commit.hexsha
        dirty = engine_repo.is_dirty(untracked_files=True)
    profile = {
        "format_version": 1, "run_id": root.name, "started_at": _now(), "backend": "local_fixture",
        "real_model_called": False, "model_name": backend.name, "model_settings": {},
        "dataset_fingerprint": bundle.fingerprint, "dataset_id": bundle.manifest.dataset_id,
        "runtime_commit": runtime_commit, "runtime_dirty": dirty,
        "runtime_file_sha256": {name: digest((ROOT / name).read_bytes()) for name in RUNTIME_FILES},
        "python": platform.python_version(),
        "dependencies": dict(sorted((dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions() if dist.metadata["Name"])),
        "source_versions": [source.model_dump() for source in bundle.manifest.sources],
        "index": {"status": "not_built", "source_only": True, "collection_token_role": "conversation_placeholder_not_vector_collection"},
        "answer_cache": "disabled_by_source_only_context", "conversation_store": "isolated_in_this_run",
        "execution_policy": "sequential_stop_on_first_error_no_retry_no_resume", "turn_timeout_seconds": turn_timeout,
        "usage_basis": "local_framework_estimate_not_provider_billing", "cost": None,
        "injected_fault_question_sha256": digest(backend.fail_question.encode()) if isinstance(backend, LocalModel) and backend.fail_question else None,
    }
    if indexed:
        profile.update(indexed["profile"])
        profile["real_model_called"] = False
        profile["runtime_file_sha256"].update({name: digest((ROOT / name).read_bytes()) for name in
            ("evaluation/real_preflight.py", "evaluation/indexes.py", "evaluation/provider_model.py", "evaluation/real_runner.py")})
        (root / "baseline").mkdir()
        from evaluation.real_preflight import BASE_FILES
        for name in BASE_FILES:
            shutil.copyfile(Path(indexed["baseline_directory"]) / name, root / "baseline" / name)
        write_json(root / "baseline-receipt.json", indexed["baseline"])
        write_json(root / "index-manifest.json", indexed["indexes"])
    write_json(root / "started.json", profile)
    # Each run exports exactly the selected committed source, never the dirty checkout.
    contexts = {}
    for source in bundle.manifest.sources:
        snapshot = create_snapshot(source.repo_url, str(repository), source.commit_sha, storage_dir=root / "_state/sources")
        token = next(row["collection_name"] for row in indexed["indexes"]["sources"] if row["source_id"] == source.source_id) if indexed else f"fixture-source-only-{source.commit_sha}"
        contexts[source.source_id] = (snapshot, token)
    agent = qa_agent.build_agent(backend.model())
    reviews, stopped, model_attempts = [], False, 0
    previous_allow = models.ALLOW_MODEL_REQUESTS
    if not indexed:
        models.ALLOW_MODEL_REQUESTS = False
    try:
        for case_number, case in enumerate(inputs, 1):
            source = case["source"]
            snapshot, scope_token = contexts[source["source_id"]]
            session = create_conversation(source["repo_url"], source["commit_sha"], scope_token,
                                          storage_dir=root / "_state/conversations")
            blocked = False
            for turn_number, turn in enumerate(case["turns"], 1):
                folder = root / "turns" / f"{case_number:03d}-{turn_number:02d}"
                folder.mkdir(parents=True)
                write_json(folder / "input.json", {"case_id": case["case_id"], "turn": turn,
                           "source": source, "conversation_id": session, "scope_token": scope_token})
                review = dict(case_id=case["case_id"], turn_id=turn["turn_id"], status="not_run", ratings=None,
                              fatal_errors=[], output_sha256=None, reviewer=None, rationale="", error=None)
                if blocked:
                    reason = "BLOCKED: previous turn failed; this turn was not executed"
                    write_json(folder / "error.json", {"kind": "dependency_blocked", "message": reason})
                    write_json(folder / "outcome.json", {"status": "blocked", "attempts": 0})
                    review.update(status="execution_failed", error=reason)
                elif stopped:
                    write_json(folder / "outcome.json", {"status": "not_run", "attempts": 0})
                else:
                    backend.begin_turn()
                    result, checked, error = [], None, None
                    started = time.perf_counter()
                    with capture_run_messages() as messages:
                        try:
                            deps = RepoDeps(source["repo_url"], snapshot.root, scope_token, source["commit_sha"], source_only=not bool(indexed), use_answer_cache=not bool(indexed),
                                            retrieval_strategy=indexed["profile"].get("retrieval_strategy", "semantic") if indexed else "semantic")
                            checked = await asyncio.wait_for(qa_agent._run_answer(
                                turn["question"], deps, session, turn["mode"], agent=agent,
                                conversation_dir=root / "_state/conversations", on_run=result.append), turn_timeout)
                        except Exception as exc:
                            error = {"kind": type(exc).__name__, "message": f"{type(exc).__name__}: {exc}",
                                     "raw_message": str(exc), "traceback": traceback.format_exc()}
                    elapsed = time.perf_counter() - started
                    model_attempts += len(backend.requests)
                    if indexed:
                        # Preserve SDK errors while removing configured credentials from every artifact.
                        from config import OPENAI_API_KEY, ANTHROPIC_API_KEY
                        secret_values = [value for value in (OPENAI_API_KEY, ANTHROPIC_API_KEY, indexed["secret"]) if value]
                        def safe(value):
                            data = json.dumps(value, ensure_ascii=False, default=str)
                            for secret in secret_values:
                                data = data.replace(secret, "<redacted>")
                            return json.loads(data)
                        messages_data = safe(_native(messages))
                        write_json(folder / "responses.json", safe(backend.responses))
                    else:
                        safe = lambda value: value
                        messages_data = _native(messages)
                    write_json(folder / "messages.json", messages_data)
                    write_json(folder / "requests.json", safe(backend.requests))
                    write_json(folder / "trace.json", safe(tool_trace(messages)))
                    usage = asdict(result[0].usage()) if result else None
                    metrics = {"elapsed_seconds": elapsed, "model_requests": len(backend.requests),
                               "framework_usage_estimate": usage, "provider_usage": None, "cost": None}
                    if indexed:
                        metrics["provider_usage"] = [item["message"]["usage"] for item in safe(backend.responses)]
                    write_json(folder / "metrics.json", metrics)
                    if result:
                        write_json(folder / "native_output.json", safe(result[0].output.model_dump(mode="json")
                                   if hasattr(result[0].output, "model_dump") else result[0].output))
                        write_json(folder / "new_messages.json", safe(_native(result[0].new_messages())))
                    if error:
                        write_json(folder / "error.json", safe(error))
                        write_json(folder / "outcome.json", {"status": "failed", "attempts": 1})
                        review.update(status="execution_failed", error=safe(error)["message"])
                        stopped, blocked = True, True
                    else:
                        write_json(folder / "output.json", safe(checked.model_dump(mode="json")))
                        write_json(folder / "outcome.json", {"status": "completed", "attempts": 1})
                        review.update(status="pending_review", output_sha256=digest((folder / "output.json").read_bytes()))
                reviews.append(review)
                # Append-only progress survives a later interrupted/incomplete run.
                with (root / "progress.jsonl").open("a", encoding="utf-8") as progress:
                    progress.write(json.dumps({"case_id": case["case_id"], "turn_id": turn["turn_id"],
                        "status": read_json_outcome(folder), "at": _now()}, ensure_ascii=False) + "\n")
    finally:
        models.ALLOW_MODEL_REQUESTS = previous_allow
    write_rows(root / "reviews.jsonl", reviews)
    write_json(root / "summary.json", summarize_reviews(bundle, reviews))
    write_json(root / "run.json", dict(profile, real_model_called=profile["backend"] == "siliconflow" and model_attempts > 0,
                                      finished_at=_now(), status="failed" if stopped else "completed"))
    seal(root)
    return verify_archive(root, repository)


def read_json_outcome(folder):
    return json.loads((folder / "outcome.json").read_text())["status"]
