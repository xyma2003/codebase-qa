"""Read bounded, numbered excerpts without leaving the selected repository."""

from dataclasses import dataclass
import hashlib
from pathlib import Path, PureWindowsPath

MAX_READ_CHARS = 8000


class SourceReadError(ValueError):
    pass


@dataclass(frozen=True)
class SourceExcerpt:
    file_path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    file_sha256: str
    truncated: bool = False
    partial_last_line: bool = False

    def format(self) -> str:
        if not self.total_lines:
            return f"**{self.file_path}**\nFile is empty."
        numbered = "\n".join(
            f"{number} | {line}"
            for number, line in enumerate(self.content.split("\n"), self.start_line)
        )
        result = (
            f"**{self.file_path}** (lines {self.start_line}–{self.end_line}, "
            f"{self.total_lines} total)\n```\n{numbered}\n```"
        )
        if self.partial_last_line:
            result += f"\nContent truncated at {MAX_READ_CHARS} characters; the last line is partial."
        elif self.truncated:
            result += f"\nContent truncated; continue with start_line={self.end_line + 1}."
        return result


def read_source_bytes(repo_root: str, file_path: str, max_bytes: int | None = None) -> tuple[str, bytes]:
    """Shared path checks; optional byte bound for whole-file source scanning."""
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise SourceReadError("max_bytes must be a positive integer.")
    if not file_path or "\x00" in file_path:
        raise SourceReadError("A repository-relative file path is required.")
    relative = Path(file_path)
    if relative.is_absolute() or PureWindowsPath(file_path).drive or ".." in relative.parts:
        raise SourceReadError("Only repository-relative paths without '..' are allowed.")
    if any(part.lower() == ".git" for part in relative.parts):
        raise SourceReadError("Git metadata cannot be read through this tool.")
    try:
        root = Path(repo_root).resolve(strict=True)
        if not root.is_dir():
            raise SourceReadError("Repository directory not found.")
        candidate = root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise SourceReadError("Symbolic links cannot be read through this tool.")
        path = candidate.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise SourceReadError("Path must point to a regular file inside the repository.")
        with path.open("rb") as source:
            data = source.read() if max_bytes is None else source.read(max_bytes + 1)
    except (OSError, RuntimeError) as exc:
        raise SourceReadError("File is missing or cannot be read.") from exc
    if max_bytes is not None and len(data) > max_bytes:
        raise SourceReadError("File exceeds the source scanning byte limit.")
    return path.relative_to(root).as_posix(), data


def read_source_file(
    repo_root: str, file_path: str, start_line: int = 1, end_line: int | None = None
) -> SourceExcerpt:
    if type(start_line) is not int or start_line < 1:
        raise SourceReadError("start_line must be a positive integer.")
    if end_line is not None and (type(end_line) is not int or end_line < start_line):
        raise SourceReadError("end_line must be an integer greater than or equal to start_line.")
    name, data = read_source_bytes(repo_root, file_path)
    if b"\x00" in data:
        raise SourceReadError("Binary files cannot be read as source text.")
    lines = data.decode("utf-8", errors="ignore").splitlines()
    digest = hashlib.sha256(data).hexdigest()
    if not lines:
        if start_line != 1 or end_line is not None:
            raise SourceReadError("The file is empty and has no line range.")
        return SourceExcerpt(name, 0, 0, 0, "", digest)
    if start_line > len(lines):
        raise SourceReadError(f"start_line exceeds the file length ({len(lines)} lines).")
    stop = min(end_line if end_line is not None else len(lines), len(lines))
    selected = []
    used = 0
    partial = False
    for line in lines[start_line - 1:stop]:
        remaining = MAX_READ_CHARS - used
        if remaining <= 0:
            break
        selected.append(line[:remaining])
        used += len(selected[-1]) + 1
        if len(selected[-1]) < len(line):
            partial = True
            break
    actual_end = start_line + len(selected) - 1
    return SourceExcerpt(
        name, start_line, actual_end, len(lines), "\n".join(selected), digest,
        truncated=partial or actual_end < stop, partial_last_line=partial,
    )
