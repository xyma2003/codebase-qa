import os
from dataclasses import dataclass
from pydantic_ai import RunContext
from indexer.vector_store import search, list_files


@dataclass
class RepoDeps:
    repo_url: str
    repo_root: str


def make_tools(agent):
    """Register all tools onto a Pydantic AI agent."""

    @agent.tool
    async def search_code(ctx: RunContext[RepoDeps], query: str) -> str:
        """
        Semantically search the codebase for code relevant to a query.
        Use this to find functions, classes, or logic related to a concept.
        Returns the top matching code snippets with file paths and line numbers.
        """
        results = search(query, ctx.deps.repo_url, n_results=5)
        if not results:
            return "No relevant code found for that query."

        output = []
        for r in results:
            output.append(
                f"**{r['file_path']}** (lines {r['start_line']}–{r['end_line']}, "
                f"{r['chunk_type']}: `{r['name']}`)\n```\n{r['content']}\n```"
            )
        return "\n\n---\n\n".join(output)

    @agent.tool
    async def list_repo_files(ctx: RunContext[RepoDeps], pattern: str = "") -> str:
        """
        List all files in the indexed repository.
        Optionally filter by a substring pattern (e.g. 'test', '.py', 'auth').
        Use this to explore the repo structure before diving into code.
        """
        files = list_files(ctx.deps.repo_url, pattern or None)
        if not files:
            return "No files found matching that pattern."
        return "\n".join(files)

    @agent.tool
    async def read_file(ctx: RunContext[RepoDeps], file_path: str) -> str:
        """
        Read the full contents of a specific file in the repository.
        Use this when you need the complete file, not just a matching snippet.
        file_path should be a relative path as returned by list_repo_files.
        """
        full_path = os.path.join(ctx.deps.repo_root, file_path)
        if not os.path.exists(full_path):
            return f"File not found: {file_path}"
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if len(content) > 8000:
                content = content[:8000] + f"\n\n... (truncated, {len(content)} chars total)"
            return f"**{file_path}**\n```\n{content}\n```"
        except Exception as e:
            return f"Error reading {file_path}: {e}"
