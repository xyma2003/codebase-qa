"""Repository facts and static Python entry clues; never execute target code."""

import ast
from collections import Counter
import hashlib
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict

from indexer.citations import SourceCitation
from indexer.code_search import MAX_FILE_BYTES, MAX_SCAN_BYTES, MAX_SCAN_FILES, SKIP_DIRECTORIES
from indexer.snapshots import load_snapshot, normalize_repo_url
from indexer.source_reader import MAX_READ_CHARS, SourceReadError, read_source_bytes

MAX_MODULES = 30
MAX_ENTRIES = 20
MAX_DIRECTORIES = 30
CONFIG_NAMES = {"config.py", "settings.py", "pyproject.toml", "requirements.txt", "setup.cfg",
                "setup.py", "package.json", "go.mod", "cargo.toml", "dockerfile", ".env.example"}


class DirectoryInfo(BaseModel):
    name: str
    file_count: int


class ModuleInfo(BaseModel):
    file_path: str
    definitions: tuple[str, ...]
    imports: tuple[str, ...]
    docstring_excerpt: str
    file_sha256: str
    definitions_truncated: bool = False
    imports_truncated: bool = False


class EntryClue(BaseModel):
    kind: str
    evidence: SourceCitation


class OverviewSkip(BaseModel):
    file_path: str
    reason: str


class RepositoryOverview(BaseModel):
    model_config = ConfigDict(frozen=True)
    repo_url: str
    commit_sha: str
    file_count: int
    python_file_count: int
    directories: tuple[DirectoryInfo, ...] = ()
    directories_truncated: bool = False
    configuration_files: tuple[str, ...] = ()
    configuration_files_truncated: bool = False
    readme: SourceCitation | None = None
    modules: tuple[ModuleInfo, ...] = ()
    modules_truncated: bool = False
    entry_clues: tuple[EntryClue, ...] = ()
    entry_clues_truncated: bool = False
    scanned_files: int = 0
    scan_limited: bool = False
    skipped_count: int = 0
    skipped_examples: tuple[OverviewSkip, ...] = ()


def _is_main_guard(node):
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    def is_name(value):
        return isinstance(value, ast.Name) and value.id == "__name__"
    def is_main(value):
        return isinstance(value, ast.Constant) and value.value == "__main__"
    return (is_name(test.left) and is_main(test.comparators[0])) or (is_main(test.left) and is_name(test.comparators[0]))


def _streamlit_config(node, aliases, direct):
    call = node.value if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)) else None
    if not isinstance(call, ast.Call):
        return False
    function = call.func
    return ((isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
             and function.value.id in aliases and function.attr == "set_page_config")
            or (isinstance(function, ast.Name) and function.id in direct))


def build_repository_overview(repo_root: str, repo_url: str, commit_sha: str) -> RepositoryOverview:
    snapshot = load_snapshot(repo_root)
    if snapshot.repo_url != normalize_repo_url(repo_url) or snapshot.commit_sha != commit_sha:
        raise SourceReadError("Overview repository and snapshot version differ.")
    paths = sorted(path for path in snapshot.files if not SKIP_DIRECTORIES.intersection(PurePosixPath(path).parts[:-1]))
    directory_counts = Counter("./" if "/" not in path else path.split("/", 1)[0] + "/" for path in paths)
    directories = tuple(DirectoryInfo(name=name, file_count=count) for name, count in sorted(directory_counts.items()))
    configs = tuple(path for path in paths if PurePosixPath(path).name.lower() in CONFIG_NAMES)
    python_files = [path for path in paths if PurePosixPath(path).suffix.lower() == ".py"]
    readmes = sorted((path for path in paths if "/" not in path and path.lower() in {"readme.md", "readme.rst", "readme.txt", "readme"}),
                     key=lambda path: ({"readme.md": 0, "readme.rst": 1, "readme.txt": 2, "readme": 3}[path.lower()], path))
    candidates = readmes[:1] + python_files
    modules, entries, skips = [], [], []
    readme = None
    bytes_read = scanned = skipped_count = 0
    scan_limited = False
    modules_truncated = entries_truncated = False

    def skip(path, reason):
        nonlocal skipped_count
        skipped_count += 1
        if len(skips) < 10:
            skips.append(OverviewSkip(file_path=path, reason=reason))

    for path in candidates:
        if scanned >= MAX_SCAN_FILES:
            scan_limited = True
            break
        scanned += 1
        size = snapshot.files[path]["bytes"]
        if size > MAX_FILE_BYTES:
            skip(path, "file_too_large")
            continue
        if bytes_read >= MAX_SCAN_BYTES or bytes_read + size > MAX_SCAN_BYTES:
            scan_limited = True
            break
        name, data = read_source_bytes(snapshot.root, path, min(MAX_FILE_BYTES, MAX_SCAN_BYTES - bytes_read))
        bytes_read += len(data)
        digest = hashlib.sha256(data).hexdigest()
        if name != path or digest != snapshot.files[path]["sha256"]:
            raise SourceReadError("Snapshot source changed; overview was not completed.")
        if b"\x00" in data:
            skip(path, "binary_file")
            continue
        text = data.decode("utf-8", errors="ignore")
        lines = text.splitlines()

        def evidence(start, end):
            # Build from the exact bytes already verified above. Keep complete lines only.
            selected = []
            length = 0
            for line in lines[start - 1:end]:
                if length >= MAX_READ_CHARS or len(line) > MAX_READ_CHARS - length:
                    break
                selected.append(line)
                length += len(line) + 1
            if not selected:
                return None
            return SourceCitation(repo_url=snapshot.repo_url, commit_sha=snapshot.commit_sha,
                                  file_path=path, start_line=start, end_line=start + len(selected) - 1,
                                  source_text="\n".join(selected), file_sha256=digest)

        if path in readmes:
            readme = evidence(1, min(len(lines), 20))
            if readme is None and lines:
                skip(path, "readme_excerpt_too_long")
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, RecursionError):
            skip(path, "python_parse_error")
            continue
        imports, streamlit_aliases, direct_config = [], set(), set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
                streamlit_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "streamlit")
            elif isinstance(node, ast.ImportFrom):
                imports.append("." * node.level + (node.module or ""))
                if node.module == "streamlit" and not node.level:
                    direct_config.update(alias.asname or alias.name for alias in node.names if alias.name == "set_page_config")
        if len(modules) < MAX_MODULES:
            definitions = tuple(node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            unique_imports = tuple(dict.fromkeys(imports))
            modules.append(ModuleInfo(
                file_path=path, definitions=definitions[:20], imports=unique_imports[:20],
                docstring_excerpt=(ast.get_docstring(tree) or "")[:300], file_sha256=digest,
                definitions_truncated=len(definitions) > 20, imports_truncated=len(unique_imports) > 20,
            ))
        else:
            modules_truncated = True
        clues = []
        if PurePosixPath(path).name == "__main__.py" and lines:
            clues.append(("package_main", 1, min(len(lines), 10)))
        for node in tree.body:
            if _is_main_guard(node):
                clues.append(("main_guard", node.lineno, min(node.end_lineno, node.lineno + 3)))
            elif _streamlit_config(node, streamlit_aliases, direct_config):
                clues.append(("streamlit_page", node.lineno, min(node.end_lineno, node.lineno + 3)))
        for kind, start, end in clues:
            source = evidence(start, end)
            if source is None:
                skip(path, "entry_excerpt_too_long")
            elif len(entries) < MAX_ENTRIES:
                entries.append(EntryClue(kind=kind, evidence=source))
            else:
                entries_truncated = True
    return RepositoryOverview(
        repo_url=snapshot.repo_url, commit_sha=snapshot.commit_sha, file_count=len(paths), python_file_count=len(python_files),
        directories=directories[:MAX_DIRECTORIES], directories_truncated=len(directories) > MAX_DIRECTORIES,
        configuration_files=configs[:30], configuration_files_truncated=len(configs) > 30,
        readme=readme, modules=tuple(modules), modules_truncated=modules_truncated,
        entry_clues=tuple(entries), entry_clues_truncated=entries_truncated,
        scanned_files=scanned, scan_limited=scan_limited, skipped_count=skipped_count, skipped_examples=tuple(skips),
    )


def overview_payload(overview):
    """Include fixed source URLs in the API payload; the UI uses typed citations."""
    result = overview.model_dump(mode="json")
    if overview.readme:
        result["readme"]["url"] = overview.readme.url
    for payload, clue in zip(result["entry_clues"], overview.entry_clues):
        payload["evidence"]["url"] = clue.evidence.url
    return result
