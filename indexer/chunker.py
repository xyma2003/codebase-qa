import ast
import os
from dataclasses import dataclass
from typing import Iterator
from config import CODE_EXTENSIONS, MAX_CHUNK_TOKENS


@dataclass
class CodeChunk:
    content: str
    file_path: str       # relative path from repo root
    start_line: int
    end_line: int
    chunk_type: str      # "function", "class", "module", "other"
    name: str            # function/class name, or filename


def _sliding_window(content: str, file_path: str) -> list[CodeChunk]:
    """Fallback chunker: split by lines into MAX_CHUNK_TOKENS-sized windows."""
    lines = content.splitlines()
    # Rough estimate: 1 token ≈ 4 chars
    max_chars = MAX_CHUNK_TOKENS * 4
    chunks = []
    start = 0
    while start < len(lines):
        chunk_lines = []
        char_count = 0
        end = start
        while end < len(lines) and char_count < max_chars:
            chunk_lines.append(lines[end])
            char_count += len(lines[end])
            end += 1
        chunks.append(CodeChunk(
            content="\n".join(chunk_lines),
            file_path=file_path,
            start_line=start + 1,
            end_line=end,
            chunk_type="other",
            name=os.path.basename(file_path),
        ))
        start = end
    return chunks


def _parse_python(content: str, file_path: str) -> list[CodeChunk]:
    """AST-based chunker for Python: extract top-level functions and classes."""
    chunks = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _sliding_window(content, file_path)

    lines = content.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Only top-level and one-level-deep definitions
            start = node.lineno - 1
            end = node.end_lineno
            chunk_content = "\n".join(lines[start:end])
            chunk_type = "class" if isinstance(node, ast.ClassDef) else "function"
            chunks.append(CodeChunk(
                content=chunk_content,
                file_path=file_path,
                start_line=node.lineno,
                end_line=node.end_lineno,
                chunk_type=chunk_type,
                name=node.name,
            ))

    # If no functions/classes found, treat whole file as one chunk
    if not chunks:
        chunks = _sliding_window(content, file_path)

    return chunks


def chunk_file(file_path: str, repo_root: str) -> list[CodeChunk]:
    """Chunk a single file. Returns empty list if file should be skipped."""
    rel_path = os.path.relpath(file_path, repo_root)
    ext = os.path.splitext(file_path)[1].lower()

    if ext not in CODE_EXTENSIONS:
        return []

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []

    if not content.strip():
        return []

    if ext == ".py":
        return _parse_python(content, rel_path)

    return _sliding_window(content, rel_path)


def chunk_repo(repo_root: str) -> Iterator[CodeChunk]:
    """Walk entire repo and yield chunks for all code files."""
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv",
                 "dist", "build", ".next", "vendor"}

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]

        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            for chunk in chunk_file(full_path, repo_root):
                yield chunk
