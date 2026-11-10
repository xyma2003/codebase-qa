"""
mcp_server.py — MCP server exposing codebase-qa tools to any MCP client.

This lets Claude Desktop, Cursor, Cline, or any MCP-compatible client
search and read code in indexed GitHub repositories.

Configuration (Claude Desktop claude_desktop_config.json):
{
  "mcpServers": {
    "codebase-qa": {
      "command": "python",
      "args": ["path/to/codebase-qa/mcp_server.py"]
    }
  }
}

Before using tools, call set_repo to import or select a version. Refreshing
the remote default branch is explicit; indexed old versions remain available.
"""
import os
import sys

# Ensure local imports work when run from any directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from indexer.vector_store import search, list_files
from indexer.repository_service import import_repository, list_repository_versions
from memory.knowledge import search_memory
from indexer.source_reader import SourceReadError, read_source_file

mcp = FastMCP("codebase-qa")

# Track the currently active repo (set by the client via set_repo tool)
_current_repo_url: str = ""
_current_repo_root: str = ""
_current_repository = None


@mcp.tool()
def set_repo(repo_url: str, refresh: bool = False, commit_sha: str | None = None) -> str:
    """Set the active repository for code search and file reading.

    Reuse a ready code version, import a new repository, or explicitly refresh
    the remote default branch. commit_sha selects a previously indexed version.

    Args:
        repo_url: GitHub repository URL (e.g. https://github.com/owner/repo)
        refresh: Fetch the remote default branch and publish only after indexing succeeds.
        commit_sha: Full SHA of an already indexed version; cannot be combined with refresh.

    Returns:
        Confirmation message with the number of indexed files.
    """
    global _current_repo_url, _current_repo_root, _current_repository
    try:
        repository = import_repository(repo_url, refresh=refresh, commit_sha=commit_sha)
    except Exception as exc:
        _current_repository = None
        _current_repo_url = _current_repo_root = ""
        return f"Failed to select repository: {exc}"
    _current_repository = repository
    _current_repo_url = repository.repo_url
    _current_repo_root = repository.repo_root
    return f"Active repo: {repository.repo_url} ({repository.file_count} files indexed). Commit: {repository.commit_sha}"


@mcp.tool()
def list_repo_versions() -> str:
    """List successfully indexed versions of the selected repository."""
    if not _current_repo_url:
        return "No repo set. Call set_repo first with a GitHub URL."
    return "\n".join(
        f"{version.commit_sha} ({version.file_count} files, {version.chunk_count} chunks)"
        for version in list_repository_versions(_current_repo_url)
    )


@mcp.tool()
def search_code(query: str) -> str:
    """Semantically search the codebase for code relevant to a query.

    Finds functions, classes, or logic related to a concept.
    Returns the top 5 matching code snippets with file paths and line numbers.
    Also checks accumulated knowledge from past Q&A.

    Args:
        query: What to search for (e.g. "user authentication", "database connection")

    Returns:
        Matching code snippets or knowledge base hits.
    """
    if not _current_repo_url:
        return "No repo set. Call set_repo first with a GitHub URL."

    # Check memory (accumulated Q&A knowledge) first
    revision = _current_repository.commit_sha if _current_repository else None
    collection = _current_repository.collection_name if _current_repository else None
    memory_hit = search_memory(query, _current_repo_url, commit_sha=revision)
    if memory_hit:
        return (
            (f"Commit: {revision}\n" if revision else "") +
            f"From knowledge base (similarity {memory_hit['score']}, "
            f"used {memory_hit['hit_count']} times):\n\n"
            f"{memory_hit['answer']}"
        )

    # Semantic code search
    results = search(query, _current_repo_url, n_results=5, collection_name=collection)
    if not results:
        return "No relevant code found for that query."

    output = []
    for r in results:
        output.append(
            f"{r['file_path']} (lines {r['start_line']}–{r['end_line']}, "
            f"{r['chunk_type']}: {r['name']})\n{r['content']}"
        )
    return (f"Commit: {revision}\n" if revision else "") + "\n\n---\n\n".join(output)


@mcp.tool()
def list_repo_files(pattern: str = "") -> str:
    """List all files in the indexed repository.

    Optionally filter by a substring pattern.

    Args:
        pattern: Optional substring to filter files (e.g. 'test', '.py', 'auth')

    Returns:
        List of file paths, one per line.
    """
    if not _current_repo_url:
        return "No repo set. Call set_repo first with a GitHub URL."

    files = list_files(
        _current_repo_url, pattern or None,
        collection_name=_current_repository.collection_name if _current_repository else None,
    )
    if not files:
        return "No files found matching that pattern."
    return "\n".join(files)


@mcp.tool()
def read_file(file_path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Read numbered source lines from a repository-relative file.

    Args:
        file_path: Relative path to the file (e.g. 'src/main.py')
        start_line: First line, 1-based and inclusive.
        end_line: Last line, inclusive. Omit to read toward the end.

    Returns:
        Numbered lines, bounded by the existing 8000-character content limit.
    """
    if not _current_repo_root:
        return "No repo set. Call set_repo first with a GitHub URL."

    try:
        if _current_repository:
            from indexer.snapshots import load_snapshot
            snapshot = load_snapshot(_current_repo_root)
            if snapshot.commit_sha != _current_repository.commit_sha or snapshot.repo_url != _current_repo_url:
                raise SourceReadError("Repository and snapshot version differ.")
            return f"Commit: {snapshot.commit_sha}\n" + snapshot.read(file_path, start_line, end_line).format()
        return read_source_file(_current_repo_root, file_path, start_line, end_line).format()
    except SourceReadError as exc:
        return f"Error reading {file_path}: {exc}"


if __name__ == "__main__":
    mcp.run()
