"""Check exact source passages against a selected, committed snapshot."""

from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from indexer.snapshots import load_snapshot, normalize_repo_url
from indexer.source_reader import SourceReadError


class SourceCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    repo_url: str
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    source_text: str
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def url(self):
        path = quote(self.file_path, safe="/")
        return f"{self.repo_url}/blob/{self.commit_sha}/{path}#L{self.start_line}-L{self.end_line}"

    @property
    def label(self):
        return f"{self.file_path}:{self.start_line}-{self.end_line}"

    @property
    def numbered_source(self):
        return "\n".join(f"{number} | {line}" for number, line in enumerate(self.source_text.split("\n"), self.start_line))


def check_source_citation(
    repo_root: str, repo_url: str, commit_sha: str, file_path: str,
    start_line: int, end_line: int, source_text: str,
    file_sha256: str | None = None,
) -> SourceCitation:
    """No clamped ranges, partial lines, approximate quotes or cross-version reuse."""
    if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line:
        raise SourceReadError("引用行号必须是有效的正整数范围。")
    if not isinstance(source_text, str):
        raise SourceReadError("引用原文必须是文本。")
    snapshot = load_snapshot(repo_root)
    if snapshot.repo_url != normalize_repo_url(repo_url) or snapshot.commit_sha != commit_sha:
        raise SourceReadError("引用与当前仓库或代码版本不一致。")
    excerpt = snapshot.read(file_path, start_line, end_line)
    if excerpt.end_line != end_line or excerpt.truncated or not excerpt.total_lines:
        raise SourceReadError("引用超出文件范围或读取上限，请缩小行号范围。")
    if excerpt.content != source_text:
        raise SourceReadError("引用原文与对应行的源码不一致。")
    if file_sha256 is not None and excerpt.file_sha256 != file_sha256:
        raise SourceReadError("引用文件的内容校验值不一致。")
    return SourceCitation(
        repo_url=snapshot.repo_url, commit_sha=commit_sha, file_path=excerpt.file_path,
        start_line=start_line, end_line=end_line, source_text=excerpt.content,
        file_sha256=excerpt.file_sha256,
    )
