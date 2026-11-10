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

Before using tools, call set_repo with a repo URL that has been indexed
via the Streamlit app (streamlit run app.py).
"""
import os
import sys

# Ensure local imports work when run from any directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from indexer.vector_store import search, list_files, is_indexed
from indexer.cloner import clone_repo
from memory.knowledge import search_memory
from indexer.source_reader import SourceReadError, read_source_file

mcp = FastMCP("codebase-qa")

# Track the currently active repo (set by the client via set_repo tool)
_current_repo_url: str = ""
_current_repo_root: str = ""


@mcp.tool()
def set_repo(repo_url: str) -> str:
    """Set the active repository for code search and file reading.

    The repo must already be indexed. If not indexed, it will be cloned
    and indexed automatically (this may take 1-3 minutes for large repos).

    Args:
        repo_url: GitHub repository URL (e.g. https://github.com/owner/repo)

    Returns:
        Confirmation message with the number of indexed files.
    """
    global _current_repo_url, _current_repo_root

    if not is_indexed(repo_url):
        # Auto-clone and index
        try:
            repo_root = clone_repo(repo_url)
            from indexer.chunker import chunk_repo
            from indexer.vector_store import index_chunks
            chunks = list(chunk_repo(repo_root))
            index_chunks(chunks, repo_url)
            _current_repo_root = repo_root
        except Exception as e:
            return f"Failed to index {repo_url}: {e}"
    else:
        # Already indexed — just clone to get the local path
        _current_repo_root = clone_repo(repo_url)

    _current_repo_url = repo_url
    files = list_files(repo_url)
    return f"Active repo: {repo_url} ({len(files)} files indexed). You can now use search_code, list_repo_files, and read_file."


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
    memory_hit = search_memory(query, _current_repo_url)
    if memory_hit:
        return (
            f"From knowledge base (similarity {memory_hit['score']}, "
            f"used {memory_hit['hit_count']} times):\n\n"
            f"{memory_hit['answer']}"
        )

    # Semantic code search
    results = search(query, _current_repo_url, n_results=5)
    if not results:
        return "No relevant code found for that query."

    output = []
    for r in results:
        output.append(
            f"{r['file_path']} (lines {r['start_line']}–{r['end_line']}, "
            f"{r['chunk_type']}: {r['name']})\n{r['content']}"
        )
    return "\n\n---\n\n".join(output)


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

    files = list_files(_current_repo_url, pattern or None)
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
        return read_source_file(_current_repo_root, file_path, start_line, end_line).format()
    except SourceReadError as exc:
        return f"Error reading {file_path}: {exc}"


if __name__ == "__main__":
    mcp.run()
