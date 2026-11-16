"""Fixed operations, canonical repository lookup, no arbitrary execution."""
from agent.runtime import RunPolicy, effective_limits
from agent.conversations import load_conversation
from indexer.snapshots import normalize_repo_url
from security.resources import check_bytes
from security.sources import ensure_safe_text
from tasks.context import check_task
from tasks.store import TaskRejected


def normalize(kind, payload):
    if not isinstance(payload, dict):
        raise TaskRejected("任务输入无效。")
    if kind == "import":
        if set(payload) - {"repo_url", "refresh"} or type(payload.get("refresh", False)) is not bool:
            raise TaskRejected("导入任务参数无效。")
        url = normalize_repo_url(payload["repo_url"])
        return {"repo_url": url, "refresh": payload.get("refresh", False)}, "import:" + url
    required = {"question", "repo_url", "commit_sha", "collection_name", "conversation_id"}
    optional = {"mode", "retrieval_strategy", "use_answer_cache", "policy"}
    if kind != "answer" or not required <= payload.keys() or set(payload) - required - optional:
        raise TaskRejected("问答任务参数无效。")
    question = payload["question"]
    if not isinstance(question, str) or not question.strip():
        raise TaskRejected("问题不能为空。")
    check_bytes(question, "问题")
    ensure_safe_text(question)
    mode, strategy = payload.get("mode", "qa"), payload.get("retrieval_strategy", "semantic")
    cache = payload.get("use_answer_cache", True)
    if mode not in {"qa", "plan"} or strategy not in {"semantic", "hybrid"} or type(cache) is not bool:
        raise TaskRejected("问答运行设置无效。")
    policy = RunPolicy.model_validate(payload["policy"]) if payload.get("policy") is not None else None
    effective_limits(policy)
    from indexer.repository_service import get_repository_version
    repository = get_repository_version(payload["repo_url"], payload["commit_sha"])
    if repository.collection_name != payload["collection_name"]:
        raise TaskRejected("任务索引与代码版本不一致。")
    conversation = load_conversation(payload["conversation_id"], repository.repo_url, repository.commit_sha, repository.collection_name)
    normalized = {key: payload[key] for key in required}
    normalized.update(repo_url=repository.repo_url, mode=mode, retrieval_strategy=strategy, use_answer_cache=cache,
                      policy=policy.model_dump(mode="json") if policy else None, revision=conversation.revision)
    return normalized, "answer:" + conversation.conversation_id


def answer(payload, progress):
    from agent.qa_agent import ask_stream
    from agent.ui_runtime import run_background
    from indexer.repository_service import get_repository_version
    check_task()
    repository = get_repository_version(payload["repo_url"], payload["commit_sha"])
    if repository.collection_name != payload["collection_name"]:
        raise TaskRejected("任务索引与代码版本不一致。")
    conversation = load_conversation(payload["conversation_id"], repository.repo_url, repository.commit_sha, repository.collection_name)
    if conversation.revision != payload["revision"]:
        raise TaskRejected("对话已更新，请重新提问。")
    checked = []
    def on_event(event):
        check_task()
        progress({"tool_name": event.tool_name, "status": event.status,
                  "elapsed_ms": event.elapsed_ms, "output_chars": event.output_chars})
    async def run():
        return "".join([part async for part in ask_stream(payload["question"], repository.repo_url, repository.repo_root,
            collection_name=repository.collection_name, commit_sha=repository.commit_sha,
            conversation_id=conversation.conversation_id, mode=payload["mode"], retrieval_strategy=payload["retrieval_strategy"],
            use_answer_cache=payload["use_answer_cache"], policy=RunPolicy.model_validate(payload["policy"]) if payload["policy"] else None,
            on_result=checked.append, on_event=on_event)])
    text = run_background(run, check_task)
    check_task()
    return {"text": text, "checked": checked[0].model_dump(mode="json") if checked else None}


def import_repo(payload, progress):
    from dataclasses import asdict
    from indexer.repository_service import import_repository
    check_task()
    progress({"stage": "importing"})
    repository = import_repository(payload["repo_url"], refresh=payload["refresh"],
        progress_callback=lambda done, total: progress({"done": done, "total": total}))
    check_task()
    return asdict(repository)
