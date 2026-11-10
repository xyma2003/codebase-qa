"""Bounded literal, path and Python definition search over committed source."""

import ast
import hashlib
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from config import CODE_EXTENSIONS
from indexer.snapshots import load_snapshot, normalize_repo_url
from indexer.source_reader import SourceReadError, read_source_bytes

SearchMode = Literal["text", "path", "symbol"]
MAX_FILE_BYTES = 1024 * 1024
MAX_SCAN_BYTES = 10 * 1024 * 1024
MAX_SCAN_FILES = 2000
MAX_PREVIEW_CHARS = 240
SKIP_DIRECTORIES = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "vendor"}


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    file_path: str
    kind: str
    start_line: int | None = None
    end_line: int | None = None
    symbol: str | None = None
    preview: str = ""
    preview_truncated: bool = False


class SkippedFile(BaseModel):
    file_path: str
    reason: str


class SearchResult(BaseModel):
    repo_url: str
    commit_sha: str
    mode: SearchMode
    query: str
    hits: tuple[SearchHit, ...]
    offset: int
    limit: int
    next_offset: int | None
    scanned_files: int
    skipped_count: int
    skipped_examples: tuple[SkippedFile, ...]
    scan_limited: bool


class _Definitions(ast.NodeVisitor):
    def __init__(self):
        self.parents = []
        self.items = []

    def _definition(self, node):
        self.parents.append(node.name)
        self.items.append((node, ".".join(self.parents)))
        self.generic_visit(node)
        self.parents.pop()

    visit_FunctionDef = _definition
    visit_AsyncFunctionDef = _definition
    visit_ClassDef = _definition


def _hits_for_file(path, text, mode, matches):
    lines = text.splitlines()
    if mode == "text":
        locations = ((number, number, None, "text") for number, line in enumerate(lines, 1) if matches(line))
    else:
        definitions = _Definitions()
        definitions.visit(ast.parse(text))
        locations = (
            (node.lineno, node.end_lineno, name, "class" if isinstance(node, ast.ClassDef) else "function")
            for node, name in sorted(definitions.items, key=lambda item: (item[0].lineno, item[1])) if matches(name)
        )
    def hit(start, end, name, kind):
        preview = lines[start - 1]
        return SearchHit(file_path=path, kind=kind, start_line=start, end_line=end, symbol=name,
                         preview=preview[:MAX_PREVIEW_CHARS], preview_truncated=len(preview) > MAX_PREVIEW_CHARS)
    return (hit(*location) for location in locations)


def find_in_snapshot(
    repo_root: str, repo_url: str, commit_sha: str, query: str,
    mode: SearchMode = "text", path_filter: str = "", case_sensitive: bool = False,
    limit: int = 20, offset: int = 0,
) -> SearchResult:
    """Literal substring matching only. Previews are locators, not checked citations."""
    if mode not in ("text", "path", "symbol"):
        raise ValueError("mode must be text, path or symbol.")
    if not isinstance(query, str) or not query.strip() or len(query) > 256 or any(char in query for char in "\x00\r\n"):
        raise ValueError("query must be nonempty, single-line text of at most 256 characters.")
    if not isinstance(path_filter, str) or len(path_filter) > 256 or any(char in path_filter for char in "\x00\r\n"):
        raise ValueError("path_filter must be single-line text of at most 256 characters.")
    if type(case_sensitive) is not bool:
        raise ValueError("case_sensitive must be a boolean.")
    if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
        raise ValueError("limit must be 1–50 and offset must be a nonnegative integer.")
    snapshot = load_snapshot(repo_root)
    if snapshot.repo_url != normalize_repo_url(repo_url) or snapshot.commit_sha != commit_sha:
        raise SourceReadError("Search repository and snapshot version differ.")
    normalize = (lambda value: value) if case_sensitive else str.casefold
    needle, path_needle = normalize(query), normalize(path_filter)
    paths = (
        path for path in sorted(snapshot.files)
        if PurePosixPath(path).suffix.lower() in CODE_EXTENSIONS
        and not SKIP_DIRECTORIES.intersection(PurePosixPath(path).parts[:-1])
        and path_needle in normalize(path)
        and (mode != "symbol" or PurePosixPath(path).suffix.lower() == ".py")
    )
    hits, skipped = [], []
    scanned, bytes_read, seen, skipped_count = 0, 0, 0, 0
    scan_limited = False
    has_more = False

    def skip(path, reason):
        nonlocal skipped_count
        skipped_count += 1
        if len(skipped) < 10:
            skipped.append(SkippedFile(file_path=path, reason=reason))

    for path in paths:
        if scanned >= MAX_SCAN_FILES:
            scan_limited = True
            break
        scanned += 1
        if mode == "path":
            candidates = iter([SearchHit(file_path=path, kind="path")]) if needle in normalize(path) else iter(())
        else:
            size = snapshot.files[path]["bytes"]
            if size > MAX_FILE_BYTES:
                skip(path, "file_too_large")
                continue
            if bytes_read >= MAX_SCAN_BYTES or bytes_read + size > MAX_SCAN_BYTES:
                scan_limited = True
                break
            name, data = read_source_bytes(snapshot.root, path, min(MAX_FILE_BYTES, MAX_SCAN_BYTES - bytes_read))
            bytes_read += len(data)
            if name != path or hashlib.sha256(data).hexdigest() != snapshot.files[path]["sha256"]:
                raise SourceReadError("Snapshot source changed; search was not completed.")
            if b"\x00" in data:
                skip(path, "binary_file")
                continue
            text = data.decode("utf-8", errors="ignore")
            try:
                candidates = _hits_for_file(path, text, mode, lambda value: needle in normalize(value))
            except (SyntaxError, RecursionError):
                skip(path, "python_parse_error")
                continue
        for candidate in candidates:
            if seen >= offset:
                if len(hits) >= limit:
                    has_more = True
                    break
                hits.append(candidate)
            seen += 1
        if has_more:
            break
    return SearchResult(
        repo_url=snapshot.repo_url, commit_sha=snapshot.commit_sha, query=query, mode=mode,
        hits=tuple(hits), offset=offset, limit=limit, next_offset=offset + len(hits) if has_more else None,
        scanned_files=scanned, skipped_count=skipped_count, skipped_examples=tuple(skipped), scan_limited=scan_limited,
    )
