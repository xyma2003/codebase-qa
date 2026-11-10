"""Materialize committed Git files independently of the mutable checkout."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import tempfile
from urllib.parse import urlsplit

from git import Repo
from config import CLONE_DIR
from indexer.source_reader import SourceReadError, read_source_file


def normalize_repo_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or parsed.query or parsed.fragment or len(parts) != 2):
        raise ValueError("请输入 https://github.com/owner/repo 格式的仓库地址。")
    owner, name = parts
    if name.lower().endswith(".git"):
        name = name[:-4]
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in (owner, name)):
        raise ValueError("仓库名称不合法。")
    return f"https://github.com/{owner.lower()}/{name.lower()}"


@dataclass(frozen=True)
class RepositorySnapshot:
    repo_url: str
    commit_sha: str
    root: str
    created_at: str
    files: dict
    skipped: tuple

    def read(self, file_path: str, start_line: int = 1, end_line: int | None = None):
        excerpt = read_source_file(self.root, file_path, start_line, end_line)
        expected = self.files.get(excerpt.file_path)
        if expected is None or excerpt.file_sha256 != expected["sha256"]:
            raise SourceReadError("Snapshot file changed or is not part of this code version.")
        return excerpt


def load_snapshot(root: str, verify: bool = False) -> RepositorySnapshot:
    path = Path(root).resolve()
    data = json.loads((path.parent / "manifest.json").read_text())
    snapshot = RepositorySnapshot(
        data["repo_url"], data["commit_sha"], str(path), data["created_at"],
        data["files"], tuple(data["skipped"]),
    )
    if not path.is_dir():
        raise ValueError("代码快照目录不存在。")
    if verify:
        for name, metadata in snapshot.files.items():
            file = path / name
            if file.is_symlink() or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != metadata["sha256"]:
                raise ValueError(f"代码快照内容不匹配：{name}")
    return snapshot


def create_snapshot(repo_url: str, checkout: str, commit_sha: str | None = None) -> RepositorySnapshot:
    repo_url = normalize_repo_url(repo_url)
    with Repo(checkout) as repo:
        return _materialize_snapshot(repo_url, repo.commit(commit_sha or "HEAD"))


def _materialize_snapshot(repo_url, commit):
    identity = hashlib.sha256(repo_url.encode()).hexdigest()[:24]
    parent = Path(CLONE_DIR).resolve() / ".snapshots" / identity
    destination = parent / commit.hexsha
    if destination.exists():
        snapshot = load_snapshot(str(destination / "source"), verify=True)
        if snapshot.repo_url != repo_url or snapshot.commit_sha != commit.hexsha:
            raise ValueError("代码快照身份不匹配。")
        return snapshot
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=parent))
    try:
        source = staging / "source"
        source.mkdir()
        files, skipped = {}, []
        for item in commit.tree.traverse():
            if item.type == "tree":
                continue
            relative = PurePosixPath(item.path)
            if relative.is_absolute() or PureWindowsPath(item.path).drive or "\\" in item.path or ".." in relative.parts or ".git" in (part.lower() for part in relative.parts):
                raise ValueError("Git 对象包含不合法路径。")
            if item.type != "blob" or item.mode not in (0o100644, 0o100755):
                skipped.append(item.path)
                continue
            target = source.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with target.open("xb") as output:
                stream = item.data_stream
                while block := stream.read(1024 * 1024):
                    output.write(block)
                    digest.update(block)
                    size += len(block)
            target.chmod(0o444)
            files[item.path] = {"sha256": digest.hexdigest(), "bytes": size}
        manifest = {
            "repo_url": repo_url, "commit_sha": commit.hexsha,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": files, "skipped": sorted(skipped),
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        try:
            staging.rename(destination)
        except OSError:
            if not destination.exists():
                raise
        return load_snapshot(str(destination / "source"), verify=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
