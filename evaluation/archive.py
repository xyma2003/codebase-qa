"""Seal and independently read back local evaluation artifacts."""

from pathlib import Path
import json
import zipfile

from git import Repo
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from agent.evidence import CheckedAnswer
from evaluation.dataset import Review, _blob, digest, read_json, read_rows, validate_bundle
from evaluation.scoring import summarize_reviews


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        file.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_rows(path, rows):
    with Path(path).open("x", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def tool_trace(messages):
    calls, returns = [], []
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                calls.append({"tool_name": part.tool_name, "tool_call_id": part.tool_call_id,
                              "arguments": part.args_as_dict()})
            elif isinstance(part, ToolReturnPart):
                returns.append({"tool_name": part.tool_name, "tool_call_id": part.tool_call_id, "content": part.content})
    return {"scope": "conversation_so_far", "calls": calls, "returns": returns}


def _files(root):
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if name == "archive.json" or name.startswith("_state/") or name == "_state":
            continue
        if path.is_symlink():
            raise ValueError("Archive artifacts cannot be symbolic links")
        if path.is_file():
            result[name] = digest(path.read_bytes())
    return result


def seal(root):
    root = Path(root)
    if (root / "archive.json").is_symlink():
        raise ValueError("Archive manifest cannot be a symbolic link")
    write_json(root / "archive.json", {"format_version": 1, "sha256": _files(root)})


def verify_archive(root, repository):
    """Do not rely on the runner's in-memory results or writable SQLite state."""
    root = Path(root)
    if (root / "archive.json").is_symlink():
        raise ValueError("Archive manifest cannot be a symbolic link")
    inventory = read_json((root / "archive.json").read_bytes())
    if inventory.get("format_version") != 1 or inventory.get("sha256") != _files(root):
        raise ValueError("Archive inventory or artifact checksum mismatch")
    run = read_json((root / "run.json").read_bytes())
    if "server_safety_limits" in run:
        from security.resources import default_run_limits
        if run["server_safety_limits"] != default_run_limits():
            raise ValueError("Unknown server safety limits")
    if type(run.get("format_version")) is not int or run["format_version"] not in {1, 2}:
        raise ValueError("Unknown evaluation archive format")
    indexed_run = run.get("format_version") == 2
    if run["status"] not in {"completed", "failed"} or (not indexed_run and (run["backend"] != "local_fixture" or run["real_model_called"])):
        raise ValueError("Not a sealed local evaluation run")
    bundle = validate_bundle(root / "dataset", repository)
    if run["dataset_fingerprint"] != bundle.fingerprint:
        raise ValueError("Archived dataset fingerprint mismatch")
    index_sources = {}
    if indexed_run:
        from evaluation.indexes import check_index_manifest
        from evaluation.real_preflight import check_baseline
        if run["backend"] not in {"siliconflow", "provider_fixture"} or type(run["real_model_called"]) is not bool or (run["backend"] == "provider_fixture" and run["real_model_called"]):
            raise ValueError("Indexed evaluation backend/provenance mismatch")
        basis = "provider_reported_tokens_cost_unknown" if run["backend"] == "siliconflow" else "fixture_provider_response"
        baseline = check_baseline(root / "baseline")
        indexes = read_json((root / "index-manifest.json").read_bytes())
        check_index_manifest(indexes, bundle, repository)
        if (read_json((root / "baseline-receipt.json").read_bytes()) != baseline
                or run["baseline_fingerprint"] != baseline["fingerprint"]
                or run["index"] != {"status": "ready", "source_only": False, "manifest_sha256": digest((root / "index-manifest.json").read_bytes())}
                or run["model_name"] != baseline["configuration"]["OPENAI_MODEL"]
                or run["provider_base_url"] != baseline["configuration"]["OPENAI_API_BASE"] or run["usage_basis"] != basis
                or run["model_settings"] != {} or run["answer_cache"] != "disabled_for_fixed_evaluation"
                or type(run["sdk_max_retries"]) is not int or run["sdk_max_retries"] < 0):
            raise ValueError("Indexed configuration differs from baseline or source-bound index")
        if read_json((root / "started.json").read_bytes()) != dict({key: value for key, value in run.items() if key not in {"finished_at", "status"}}, real_model_called=False):
            raise ValueError("Started and finished evaluation profiles differ")
        index_sources = {row["source_id"]: row for row in indexes["sources"]}
    inputs = read_json((root / "inputs.json").read_bytes())
    if inputs != bundle.model_inputs():
        raise ValueError("Archived model inputs differ from frozen inputs")
    reviews = read_rows((root / "reviews.jsonl").read_bytes(), Review)
    indexed = {(row.case_id, row.turn_id): row for row in reviews}
    if len(indexed) != len(reviews):
        raise ValueError("Duplicate review records")
    sessions, attempted, completed, failed, blocked, pending = set(), 0, 0, 0, 0, 0
    outcomes, stopped, model_attempts = [], False, 0
    with Repo(repository) as repo:
        for case_index, case in enumerate(inputs, 1):
            session, case_blocked = None, False
            for turn_index, turn in enumerate(case["turns"], 1):
                folder = root / "turns" / f"{case_index:03d}-{turn_index:02d}"
                request = read_json((folder / "input.json").read_bytes())
                outcome = read_json((folder / "outcome.json").read_bytes())
                if request["case_id"] != case["case_id"] or request["turn"] != turn or request["source"] != case["source"]:
                    raise ValueError("Archived turn input/source mismatch")
                if session is None:
                    session = request["conversation_id"]
                    if session in sessions:
                        raise ValueError("Different cases reused a conversation")
                    sessions.add(session)
                if session != request["conversation_id"]:
                    raise ValueError("A multi-turn case changed its conversation")
                review = indexed[(case["case_id"], turn["turn_id"])]
                status = outcome["status"]
                allowed = {"blocked"} if case_blocked else {"not_run"} if stopped else {"completed", "failed"}
                if status not in allowed:
                    raise ValueError("Turn sequence violates fail-fast execution policy")
                if status == "failed":
                    stopped, case_blocked = True, True
                if outcome["attempts"] != (1 if status in {"completed", "failed"} else 0):
                    raise ValueError("Unexpected retry or attempt count")
                expected_scope = index_sources[case["source"]["source_id"]]["collection_name"] if indexed_run else f"fixture-source-only-{case['source']['commit_sha']}"
                if request["scope_token"] != expected_scope:
                    raise ValueError("Source-only placeholder was misidentified")
                outcomes.append(status)
                if status in {"completed", "failed"}:
                    attempted += 1
                    messages = ModelMessagesTypeAdapter.validate_json((folder / "messages.json").read_bytes())
                    if read_json((folder / "trace.json").read_bytes()) != tool_trace(messages):
                        raise ValueError("Tool trace does not match native messages")
                    requests = read_json((folder / "requests.json").read_bytes())
                    model_attempts += len(requests)
                    if not requests and (not indexed_run or status == "completed"):
                        raise ValueError("An attempted local turn has no model request record")
                    expected_prompts = [item["question"] for item in case["turns"][:turn_index]]
                    for model_request in requests:
                        native = ModelMessagesTypeAdapter.validate_python(model_request["messages"])
                        prompts = [part.content for message in native for part in message.parts if isinstance(part, UserPromptPart)]
                        if prompts != expected_prompts:
                            raise ValueError("Native model input lost or leaked conversation context")
                    metrics = read_json((folder / "metrics.json").read_bytes())
                    if metrics["model_requests"] != len(requests) or metrics["cost"] is not None:
                        raise ValueError("Local request metrics mismatch")
                    if indexed_run:
                        responses = read_json((folder / "responses.json").read_bytes())
                        numbers = [item["request_number"] for item in responses]
                        if numbers != [number for number, item in enumerate(requests, 1) if item["status"] == "completed"]:
                            raise ValueError("Provider response/request sequence mismatch")
                        if metrics["provider_usage"] != [item["message"]["usage"] for item in responses]:
                            raise ValueError("Provider usage differs from recorded responses")
                        native_responses = [message for message in messages if isinstance(message, ModelResponse)][-len(responses):] if responses else []
                        native_data = json.loads(ModelMessagesTypeAdapter.dump_json(native_responses))
                        def comparable(item):
                            return {key: value for key, value in item.items() if key not in {"run_id", "conversation_id"}}
                        if [comparable(item["message"]) for item in responses] != [comparable(item) for item in native_data]:
                            raise ValueError("Provider responses differ from actual native messages")
                        if status == "completed" and (not responses or any(item["status"] != "completed" for item in requests)):
                            raise ValueError("Completed output includes an unsuccessful model request")
                if status == "completed":
                    completed += 1
                    if review.status != "pending_review":
                        raise ValueError("Local fixture outputs must remain unreviewed")
                    output_bytes = (folder / "output.json").read_bytes()
                    checked = CheckedAnswer.model_validate_json(output_bytes)
                    if checked.runtime is not None:
                        usage = metrics["framework_usage_estimate"]
                        record = checked.runtime
                        expected_strategy = run.get("retrieval_strategy", "semantic") if indexed_run else "semantic"
                        if (usage is None or any(getattr(record, key) != usage[key] for key in
                                                ("requests", "tool_calls", "input_tokens", "output_tokens"))
                                or record.requests != len(requests) or record.policy is not None
                                or record.retrieval_strategy != expected_strategy or record.use_answer_cache != (not indexed_run)):
                            raise ValueError("Archived runtime record differs from actual run settings/usage")
                        if "server_safety_limits" in run and record.enforced_limits != run["server_safety_limits"]:
                            raise ValueError("Archived runtime record differs from server safety limits")
                    if review.output_sha256 != digest(output_bytes):
                        raise ValueError("Review is bound to the wrong output artifact")
                    if (turn["mode"] == "plan") != (checked.plan is not None):
                        raise ValueError("Archived output mode mismatch")
                    for source in checked.citations:
                        if (source.repo_url, source.commit_sha) != (case["source"]["repo_url"], case["source"]["commit_sha"]):
                            raise ValueError("Archived citation uses another source")
                        data = _blob(repo.commit(source.commit_sha), source.file_path)
                        lines = data.decode("utf-8").splitlines()
                        if not 1 <= source.start_line <= source.end_line <= len(lines):
                            raise ValueError("Archived citation range is invalid")
                        if digest(data) != source.file_sha256 or "\n".join(lines[source.start_line-1:source.end_line]) != source.source_text:
                            raise ValueError("Archived citation content mismatch")
                    # SDK native serialization is independently readable as well.
                    new = ModelMessagesTypeAdapter.validate_json((folder / "new_messages.json").read_bytes())
                    if [part.content for message in new for part in message.parts if isinstance(part, UserPromptPart)] != [turn["question"]]:
                        raise ValueError("New messages contain duplicate or missing user turns")
                    raw = read_json((folder / "native_output.json").read_bytes())
                    if checked.plan is not None and raw != checked.plan.model_dump(mode="json"):
                        raise ValueError("Structured native output differs from archived plan")
                    response = next(message for message in reversed(messages) if isinstance(message, ModelResponse))
                    if checked.plan is not None:
                        outputs = [part.args_as_dict() for part in response.parts
                                   if isinstance(part, ToolCallPart) and part.tool_name == "submit_change_plan"]
                        if outputs != [raw]:
                            raise ValueError("Native plan differs from actual output tool call")
                    elif raw != "".join(part.content for part in response.parts if isinstance(part, TextPart)):
                        raise ValueError("Native answer differs from actual model response")
                elif status in {"failed", "blocked"}:
                    failed += status == "failed"
                    blocked += status == "blocked"
                    error = read_json((folder / "error.json").read_bytes())
                    if review.status != "execution_failed" or review.error != error["message"]:
                        raise ValueError("Failure review lost the original error/reason")
                    if status == "failed" and not error.get("traceback"):
                        raise ValueError("Original execution traceback is missing")
                    if (folder / "output.json").exists():
                        raise ValueError("Failed/blocked turn cannot publish a completed output")
                elif status == "not_run":
                    pending += 1
                    if review.status != "not_run":
                        raise ValueError("Unattempted turn has an execution or review result")
                else:
                    raise ValueError("Unknown execution status")
    expected_status = "failed" if failed else "completed"
    if indexed_run and run["real_model_called"] is not (run["backend"] == "siliconflow" and model_attempts > 0):
        raise ValueError("Real-provider request marker differs from archived requests")
    if run["status"] != expected_status or (not failed and (blocked or pending)):
        raise ValueError("Run status does not match its turns")
    progress = [read_json(line) for line in (root / "progress.jsonl").read_text().splitlines()]
    if [(row["case_id"], row["turn_id"], row["status"]) for row in progress] != [
            (case["case_id"], turn["turn_id"], status)
            for case, turn, status in zip(
                [case for case in inputs for _ in case["turns"]],
                [turn for case in inputs for turn in case["turns"]], outcomes)]:
        raise ValueError("Progress journal differs from the fixed turn sequence")
    summary = summarize_reviews(bundle, reviews)
    if read_json((root / "summary.json").read_bytes()) != summary or summary["finalized"]:
        raise ValueError("Local fixture run cannot publish a quality score")
    return {"status": "archive_verified", "backend": run["backend"], "real_model_called": run["real_model_called"],
            "required_turns": bundle.manifest.turn_count, "attempted_turns": attempted,
            "completed_turns": completed, "failed_turns": failed, "blocked_turns": blocked,
            "not_run_turns": pending, "reviewed_turns": 0, "quality_score": None,
            "dataset_fingerprint": bundle.fingerprint, "archive_sha256": digest((root / "archive.json").read_bytes()),
            "artifact_count": len(inventory["sha256"])}


def pack_archive(root, destination):
    """Pack only sealed input/output artifacts; exclude writable runtime state."""
    root, destination = Path(root), Path(destination)
    inventory = read_json((root / "archive.json").read_bytes())["sha256"]
    if inventory != _files(root) or destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("Package needs an unchanged sealed archive and an external destination")
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("archive.json", *sorted(inventory)):
            path = root / name
            if name != "archive.json" and digest(path.read_bytes()) != inventory[name]:
                raise ValueError("Artifact changed before packaging")
            archive.write(path, name)
