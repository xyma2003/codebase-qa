import os
from pydantic_ai import Agent
from agent.tools import RepoDeps, make_tools
from config import CLAUDE_MODEL, ANTHROPIC_API_KEY

_agent: Agent | None = None


def _get_agent() -> Agent:
    """懒加载 Agent，确保 API key 在 import 时不被检查。"""
    global _agent
    if _agent is not None:
        return _agent

    # 设置环境变量（优先使用 .env 中的值）
    if ANTHROPIC_API_KEY:
        os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

    _agent = Agent(
        f"anthropic:{CLAUDE_MODEL}",
        deps_type=RepoDeps,
        system_prompt=(
            "You are an expert software engineer helping developers understand a codebase. "
            "You have access to three tools:\n"
            "- search_code: semantic search over the codebase\n"
            "- list_repo_files: list all files (optionally filtered)\n"
            "- read_file: read a full file\n\n"
            "When answering questions:\n"
            "1. Use search_code first to find relevant code\n"
            "2. Use list_repo_files to explore structure when needed\n"
            "3. Use read_file when you need the full context of a file\n"
            "4. Always cite file paths and line numbers in your answers\n"
            "5. For 'how to add a feature' questions, show exactly where to make changes"
        ),
    )
    make_tools(_agent)
    return _agent


async def ask(question: str, repo_url: str, repo_root: str) -> str:
    """Run a single question against the indexed repo."""
    deps = RepoDeps(repo_url=repo_url, repo_root=repo_root)
    result = await _get_agent().run(question, deps=deps)
    return result.output


async def ask_stream(question: str, repo_url: str, repo_root: str):
    """Stream a response token by token."""
    deps = RepoDeps(repo_url=repo_url, repo_root=repo_root)
    async with _get_agent().run_stream(question, deps=deps) as result:
        async for chunk in result.stream_output(debounce_by=None):
            yield chunk
