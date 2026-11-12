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
from security.sources import MAX_SOURCE_BYTES, SourcePolicyError, ensure_safe_source, ensure_safe_text, sensitive_path
from security.resources import snapshot_budget


def normalize_repo_url(value: str) -> str:
    ensure_safe_text(value)
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
        if self.files.get(file_path, {}).get("materialized") is False:
            raise SourceReadError("文件超过源码读取安全上限。")
        excerpt = read_source_file(self.root, file_path, start_line, end_line)
        expected = self.files.get(excerpt.file_path)
        if expected is None or excerpt.file_sha256 != expected["sha256"]:
            raise SourceReadError("Snapshot file changed or is not part of this code version.")
        return excerpt


def load_snapshot(root: str, verify: bool = False) -> RepositorySnapshot:
    path = Path(root).resolve()
    with (path.parent / "manifest.json").open("rb") as file:
        raw = file.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("快照清单超过安全上限。")
    data = json.loads(raw)
    snapshot_budget(data["files"])
    for name in data["files"]:
        relative = PurePosixPath(name)
        if (relative.is_absolute() or PureWindowsPath(name).drive or "\\" in name or ".." in relative.parts
                or ".git" in (part.lower() for part in relative.parts)):
            raise ValueError("快照包含不合法路径。")
    snapshot = RepositorySnapshot(
        data["repo_url"], data["commit_sha"], str(path), data["created_at"],
        {name: meta for name, meta in data["files"].items() if not sensitive_path(name)},
        tuple(sorted(set(data["skipped"]) | {name for name in data["files"] if sensitive_path(name)})),
    )
    if not path.is_dir():
        raise ValueError("代码快照目录不存在。")
    if verify:
        for name, metadata in snapshot.files.items():
            if not metadata.get("materialized", True) or metadata["bytes"] > MAX_SOURCE_BYTES:
                continue
            file = path / name
            if file.is_symlink() or not file.is_file():
                raise ValueError(f"代码快照内容不匹配：{name}")
            with file.open("rb") as source:
                content = source.read(MAX_SOURCE_BYTES + 1)
            if len(content) != metadata["bytes"] or hashlib.sha256(content).hexdigest() != metadata["sha256"]:
                raise ValueError(f"代码快照内容不匹配：{name}")
    return snapshot


def create_snapshot(repo_url: str, checkout: str, commit_sha: str | None = None, *, storage_dir=None) -> RepositorySnapshot:
    repo_url = normalize_repo_url(repo_url)
    with Repo(checkout) as repo:
        return _materialize_snapshot(repo_url, repo.commit(commit_sha or "HEAD"), storage_dir)


def _materialize_snapshot(repo_url, commit, storage_dir=None):
    identity = hashlib.sha256(repo_url.encode()).hexdigest()[:24]
    parent = Path(storage_dir if storage_dir is not None else CLONE_DIR).resolve() / ".snapshots" / identity
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
        seen = 0
        for item in commit.tree.traverse():
            seen += 1
            from security.resources import MAX_SNAPSHOT_FILES
            if seen > MAX_SNAPSHOT_FILES * 2:
                raise ValueError("Git 目录项数量超过安全上限。")
            if item.type == "tree":
                continue
            relative = PurePosixPath(item.path)
            if relative.is_absolute() or PureWindowsPath(item.path).drive or "\\" in item.path or ".." in relative.parts or ".git" in (part.lower() for part in relative.parts):
                raise ValueError("Git 对象包含不合法路径。")
            if item.type != "blob" or item.mode not in (0o100644, 0o100755):
                skipped.append(item.path)
                continue
            if sensitive_path(item.path):
                skipped.append(item.path)
                continue
            if item.size > MAX_SOURCE_BYTES:
                files[item.path] = {"sha256": None, "bytes": item.size, "materialized": False, "git_blob_sha": item.hexsha}
                snapshot_budget(files)
                continue
            # Inspect bounded source before materializing it. Large blobs are
            # separately denied by every source/embedding entry point.
            bounded = item.data_stream.read(MAX_SOURCE_BYTES + 1)
            if len(bounded) != item.size:
                raise ValueError("Git 源文件长度校验失败。")
            try:
                ensure_safe_source(item.path, bounded)
            except SourcePolicyError:
                skipped.append(item.path)
                continue
            snapshot_budget({**files, item.path: {"bytes": len(bounded)}})
            target = source.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with target.open("xb") as output:
                output.write(bounded)
                digest.update(bounded)
                size = len(bounded)
            target.chmod(0o444)
            files[item.path] = {"sha256": digest.hexdigest(), "bytes": size}
            snapshot_budget(files)
        manifest = {
            "repo_url": repo_url, "commit_sha": commit.hexsha,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": files, "skipped": sorted(skipped),
        }
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        if len(manifest_text.encode()) > MAX_SOURCE_BYTES:
            raise ValueError("快照清单超过安全上限。")
        (staging / "manifest.json").write_text(manifest_text)
        try:
            staging.rename(destination)
        except OSError:
            if not destination.exists():
                raise
        return load_snapshot(str(destination / "source"), verify=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
