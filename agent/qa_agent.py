from typing import Literal

from pydantic_ai import Agent, ToolOutput
from agent.tools import RepoDeps, make_tools
from config import LLM_PROVIDER, OPENAI_API_KEY, OPENAI_API_BASE, OPENAI_MODEL, LLM_MODEL_STRING, QUALITY_THRESHOLD
from memory.quality import evaluate
from memory.knowledge import save_qa
from agent.evidence import CITATION_INSTRUCTIONS, CheckedAnswer, EvidenceStore
from agent.conversations import load_conversation, append_turn
from agent.planning import ChangePlan, PLANNING_INSTRUCTIONS, check_plan

AnswerMode = Literal["qa", "plan"]

_agent: Agent | None = None


def _get_agent() -> Agent:
    """懒加载 Agent，确保 API key 在 import 时不被检查。"""
    global _agent
    if _agent is not None:
        return _agent

    # Build model — for OpenAI-compatible (SiliconFlow), pass explicit base_url
    # because Pydantic AI's model-string shortcut doesn't read OPENAI_API_BASE.
    if LLM_PROVIDER == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        provider = OpenAIProvider(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)
        model = OpenAIChatModel(OPENAI_MODEL, provider=provider)
    else:
        # Anthropic — model string shortcut works fine
        model = LLM_MODEL_STRING

    _agent = build_agent(model)
    return _agent


def build_agent(model) -> Agent:
    """Build the same prompt and tools with an explicitly supplied model."""
    agent = Agent(
        model,
        deps_type=RepoDeps,
        system_prompt=(
            "You are an expert software engineer helping developers understand a codebase. "
            "You have access to these tools:\n"
            "- get_repo_overview: inspect directories, modules, README and static entry clues\n"
            "- get_file_dependencies: locate static Python import candidates and possible incoming references\n"
            "- find_code: locate literal text, file paths or Python definitions in the selected version\n"
            "- search_code: semantic search over the codebase\n"
            "- list_repo_files: list all files (optionally filtered)\n"
            "- read_file: read a full file\n\n"
            "When answering questions:\n"
            "1. Use get_repo_overview for project orientation; use find_code for known names, paths or literal text; use search_code for conceptual questions\n"
            "2. Use list_repo_files to explore structure when needed\n"
            "3. Use read_file to inspect located code and obtain source citation identifiers\n"
            "4. Always cite file paths and line numbers in your answers\n"
            "5. For 'how to add a feature' questions, show exactly where to make changes\n"
            "6. Use get_file_dependencies for cross-file import clues; distinguish static candidates from runtime behavior"
        ),
    )
    make_tools(agent)
    return agent


async def _run_answer(question, deps, conversation_id=None, mode: AnswerMode = "qa", *, agent=None, conversation_dir=None, on_run=None, on_event=None):
    if mode not in ("qa", "plan"):
        raise ValueError("回答模式无效。")
    if mode == "plan" and deps.evidence is None:
        raise ValueError("修改规划需要已选定的仓库代码版本。")
    deps.tool_events.clear()
    deps.tool_observer = on_event
    history = None
    storage = {"storage_dir": conversation_dir} if conversation_dir is not None else {}
    if conversation_id is not None:
        history = load_conversation(conversation_id, deps.repo_url, deps.commit_sha, deps.collection_name, **storage)
        deps.evidence = EvidenceStore(deps.repo_url, deps.repo_root, deps.commit_sha,
                                      reference_offset=history.reference_count)
    options = {"instructions": CITATION_INSTRUCTIONS} if deps.evidence else {}
    if mode == "plan":
        options.update(instructions=CITATION_INSTRUCTIONS + "\n" + PLANNING_INSTRUCTIONS,
                       output_type=ToolOutput(ChangePlan, name="submit_change_plan", max_retries=0))
    if history is not None:
        options.update(message_history=history.messages, conversation_id=history.conversation_id)
    result = await (agent or _get_agent()).run(question, deps=deps, **options)
    if on_run is not None:
        on_run(result)
    if mode == "plan":
        checked = check_plan(result.output, deps.evidence)
    else:
        checked = deps.evidence.check_answer(result.output) if deps.evidence else CheckedAnswer(text=result.output)
    checked = checked.model_copy(update={"tool_events": tuple(deps.tool_events)})
    if history is not None:
        append_turn(history, question, result.new_messages(), checked, deps.evidence.reference_count, **storage)
    # Keep the existing score and threshold; pinned answers additionally need checked references.
    # Follow-ups depend on conversation context and must not become standalone cached answers.
    score = evaluate(question, result.output) if mode == "qa" else 0
    if not deps.source_only and deps.use_answer_cache and mode == "qa" and (history is None or history.revision == 0) and score >= QUALITY_THRESHOLD and (deps.evidence is None or checked.can_save):
        try:
            save_qa(question, checked.text, deps.repo_url, commit_sha=deps.commit_sha)
        except Exception:
            pass  # 沉淀失败不影响主流程
    return checked


async def ask(question: str, repo_url: str, repo_root: str, *, collection_name: str | None = None, commit_sha: str | None = None, conversation_id: str | None = None, mode: AnswerMode = "qa") -> str:
    """Run a question and resolve source references against the selected version."""
    deps = RepoDeps(repo_url=repo_url, repo_root=repo_root, collection_name=collection_name, commit_sha=commit_sha)
    return (await _run_answer(question, deps, conversation_id, mode)).text


async def ask_stream(question: str, repo_url: str, repo_root: str, *, collection_name: str | None = None, commit_sha: str | None = None, conversation_id: str | None = None, mode: AnswerMode = "qa", on_result=None, on_event=None):
    """Stream a response token by token，流结束后触发知识沉淀。

    NOTE: Pydantic AI's run_stream() doesn't work with SiliconFlow's OpenAI-compatible
    API (returns empty chunks). We use run() for tool use + answer generation, then
    simulate streaming by yielding the answer in small chunks for better UX.
    """
    deps = RepoDeps(repo_url=repo_url, repo_root=repo_root, collection_name=collection_name, commit_sha=commit_sha)

    checked = await _run_answer(question, deps, conversation_id, mode, on_event=on_event)
    if on_result is not None:
        on_result(checked)
    full_response = checked.text

    # Simulate streaming: yield in ~10-char chunks for responsive UI
    chunk_size = 10
    for i in range(0, len(full_response), chunk_size):
        yield full_response[i:i + chunk_size]
