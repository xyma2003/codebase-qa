"""Locate static Python import candidates without loading target modules."""

import ast
from collections import defaultdict
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel

from indexer.citations import SourceCitation
from indexer.code_search import MAX_FILE_BYTES, MAX_SCAN_BYTES, MAX_SCAN_FILES, SKIP_DIRECTORIES
from indexer.snapshots import load_snapshot, normalize_repo_url
from indexer.source_reader import SourceReadError, read_source_bytes

DependencyDirection = Literal["outgoing", "incoming"]
MAX_IMPORT_QUOTE_CHARS = 1000


class DependencyTarget(BaseModel):
    file_path: str
    role: Literal["module", "possible_submodule"]


class ImportReference(BaseModel):
    source_file: str
    start_line: int
    end_line: int
    kind: str
    module: str
    imported_name: str | None
    alias: str | None
    context: tuple[str, ...]
    resolution: str
    member_not_followed: bool = False
    targets: tuple[DependencyTarget, ...]
    evidence: SourceCitation | None
    evidence_unavailable: bool


class DependencyNotice(BaseModel):
    file_path: str
    line: int | None = None
    reason: str


class DependencyResult(BaseModel):
    repo_url: str
    commit_sha: str
    file_path: str
    source_root: str
    direction: DependencyDirection
    references: tuple[ImportReference, ...]
    offset: int
    limit: int
    next_offset: int | None
    scanned_files: int
    scan_limited: bool
    skipped_count: int
    skipped_examples: tuple[DependencyNotice, ...]
    unresolved_count: int
    unresolved_examples: tuple[DependencyNotice, ...]


def _relative_path(value, allow_root=False):
    if not isinstance(value, str) or len(value) > 512 or any(char in value for char in "\x00\r\n\\"):
        raise SourceReadError("A repository-relative path is required.")
    path = PurePosixPath(value)
    if path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts or any(part.lower() == ".git" for part in path.parts):
        raise SourceReadError("Paths must stay inside the selected source root.")
    result = path.as_posix()
    if result == ".":
        if allow_root:
            return ""
        raise SourceReadError("A Python file path is required.")
    return result


class _Imports(ast.NodeVisitor):
    def __init__(self, tree):
        self.context, self.items = [], []
        self.importlib_aliases = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names if alias.name == "importlib"}
        self.dynamic_names = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "importlib" and node.level == 0 for alias in node.names if alias.name == "import_module"}
        self.visit(tree)

    def generic_visit(self, node):
        label = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            label = f"{type(node).__name__}:{node.name}"
        elif isinstance(node, (ast.If, ast.Try, ast.TryStar, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Match)):
            label = type(node).__name__
        if label:
            self.context.append(label)
        super().generic_visit(node)
        if label:
            self.context.pop()

    def visit_Import(self, node):
        for alias in node.names:
            self.items.append((node, "import", alias.name, None, alias.asname, 0, tuple(self.context)))

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.items.append((node, "from", node.module or "", alias.name, alias.asname, node.level, tuple(self.context)))

    def visit_Call(self, node):
        function = node.func
        dynamic = ((isinstance(function, ast.Name) and (function.id == "__import__" or function.id in self.dynamic_names))
                   or (isinstance(function, ast.Attribute) and function.attr == "import_module" and isinstance(function.value, ast.Name) and function.value.id in self.importlib_aliases))
        if dynamic:
            module = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) else ""
            self.items.append((node, "dynamic", module[:256], None, None, 0, tuple(self.context)))
        self.generic_visit(node)


def get_file_dependencies(
    repo_root: str, repo_url: str, commit_sha: str, file_path: str,
    direction: DependencyDirection = "outgoing", source_root: str = "", limit: int = 20, offset: int = 0,
) -> DependencyResult:
    """Return path candidates, not a runtime import graph or complete impact analysis."""
    if direction not in ("outgoing", "incoming"):
        raise ValueError("direction must be outgoing or incoming.")
    if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
        raise ValueError("limit must be 1–50; offset must be a nonnegative integer.")
    file_path = _relative_path(file_path)
    source_root = _relative_path(source_root, allow_root=True)
    prefix = source_root + "/" if source_root else ""
    snapshot = load_snapshot(repo_root)
    if snapshot.repo_url != normalize_repo_url(repo_url) or snapshot.commit_sha != commit_sha:
        raise SourceReadError("Dependency repository and snapshot version differ.")
    paths = sorted(path for path in snapshot.files if path.startswith(prefix) and path.endswith(".py")
                   and not SKIP_DIRECTORIES.intersection(PurePosixPath(path).parts[:-1]))
    if file_path not in paths:
        raise SourceReadError("The selected file is not a committed .py file inside source_root.")
    modules = defaultdict(list)
    for path in paths:
        relative = path[len(prefix):]
        name = relative[:-12] if relative.endswith("/__init__.py") else relative[:-3]
        if relative == "__init__.py":
            name = ""
        modules[name.replace("/", ".")].append(path)
    references, skips, unresolved = [], [], []
    skipped_count = unresolved_count = scanned = bytes_read = seen = 0
    scan_limited = has_more = False

    for path in ([file_path] if direction == "outgoing" else paths):
        if scanned >= MAX_SCAN_FILES:
            scan_limited = True
            break
        scanned += 1
        size = snapshot.files[path]["bytes"]
        reason = "file_too_large" if size > MAX_FILE_BYTES else None
        if not reason and (bytes_read >= MAX_SCAN_BYTES or bytes_read + size > MAX_SCAN_BYTES):
            scan_limited = True
            break
        if not reason:
            name, data = read_source_bytes(snapshot.root, path, min(MAX_FILE_BYTES, MAX_SCAN_BYTES - bytes_read))
            bytes_read += len(data)
            digest = hashlib.sha256(data).hexdigest()
            if name != path or digest != snapshot.files[path]["sha256"]:
                raise SourceReadError("Snapshot source changed; dependency lookup was not completed.")
            if b"\x00" in data:
                reason = "binary_file"
            else:
                text = data.decode("utf-8", errors="ignore")
                try:
                    imports = _Imports(ast.parse(text)).items
                except (SyntaxError, RecursionError):
                    reason = "python_parse_error"
        if reason:
            skipped_count += 1
            if len(skips) < 10:
                skips.append(DependencyNotice(file_path=path, reason=reason))
            continue
        lines = text.splitlines()
        for node, kind, module, member, alias, level, context in sorted(imports, key=lambda item: (item[0].lineno, item[0].col_offset)):
            resolved_module = module
            invalid_relative = False
            if level:
                package = path[len(prefix):].split("/")[:-1]
                invalid_relative = level > len(package)
                if not invalid_relative:
                    resolved_module = ".".join(package[:len(package) - level + 1] + (module.split(".") if module else []))
            targets = []
            if kind == "dynamic":
                resolution = "dynamic_not_resolved"
            elif invalid_relative:
                resolution = "relative_outside_source_root"
            else:
                base = modules.get(resolved_module, [])
                child = modules.get(resolved_module + "." + member, []) if kind == "from" and member != "*" else []
                targets = [DependencyTarget(file_path=target, role="module") for target in base]
                targets.extend(DependencyTarget(file_path=target, role="possible_submodule") for target in child)
                if member == "*":
                    resolution = "wildcard_not_expanded"
                elif len(base) > 1 or len(child) > 1 or child and base:
                    resolution = "ambiguous_candidates"
                elif child:
                    resolution = "submodule_candidate"
                elif base:
                    resolution = "local_module_candidate"
                else:
                    resolution = "module_not_located"
            uncertain = resolution != "local_module_candidate" or kind == "from"
            if uncertain:
                unresolved_count += 1
                if len(unresolved) < 10:
                    unresolved.append(DependencyNotice(file_path=path, line=node.lineno, reason=resolution if resolution != "local_module_candidate" else "imported_member_not_followed"))
            if direction == "incoming" and not any(target.file_path == file_path for target in targets):
                continue
            if seen < offset:
                seen += 1
                continue
            if len(references) >= limit:
                has_more = True
                break
            quote = "\n".join(lines[node.lineno - 1:node.end_lineno])
            evidence = SourceCitation(repo_url=snapshot.repo_url, commit_sha=snapshot.commit_sha, file_path=path,
                                      start_line=node.lineno, end_line=node.end_lineno, source_text=quote, file_sha256=digest) if len(quote) <= MAX_IMPORT_QUOTE_CHARS else None
            references.append(ImportReference(
                source_file=path, start_line=node.lineno, end_line=node.end_lineno, kind=kind,
                module="." * level + module, imported_name=member, alias=alias, context=context,
                resolution=resolution, member_not_followed=kind == "from", targets=tuple(targets),
                evidence=evidence, evidence_unavailable=evidence is None,
            ))
            seen += 1
        if has_more:
            break
    return DependencyResult(
        repo_url=snapshot.repo_url, commit_sha=snapshot.commit_sha, file_path=file_path,
        source_root=source_root, direction=direction, references=tuple(references), limit=limit, offset=offset,
        next_offset=offset + len(references) if has_more else None, scanned_files=scanned,
        scan_limited=scan_limited, skipped_count=skipped_count, skipped_examples=tuple(skips),
        unresolved_count=unresolved_count, unresolved_examples=tuple(unresolved),
    )


def dependency_payload(result):
    payload = result.model_dump(mode="json")
    for data, reference in zip(payload["references"], result.references):
        if reference.evidence:
            data["evidence"]["url"] = reference.evidence.url
    return payload
