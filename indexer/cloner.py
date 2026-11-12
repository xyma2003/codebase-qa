import os
import shutil
from pathlib import Path
import tempfile
from git import Repo
from config import CLONE_DIR
from security.git_transport import require_public_repository, run_git


def clone_repo(repo_url: str) -> str:
    """
    Clone a GitHub repo (shallow) and return the local path.
    If already cloned, return the existing path without re-cloning.
    """
    from indexer.snapshots import normalize_repo_url
    repo_url = normalize_repo_url(repo_url)
    require_public_repository(repo_url)
    os.makedirs(CLONE_DIR, exist_ok=True)

    # Derive a folder name from the URL: owner__reponame
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    owner = repo_url.rstrip("/").split("/")[-2]
    local_path = os.path.join(CLONE_DIR, f"{owner}__{repo_name}")

    if os.path.exists(local_path):
        if Path(local_path).is_symlink():
            raise ValueError("仓库缓存不能是符号链接。")
        with Repo(local_path) as repo:
            if normalize_repo_url(repo.remotes.origin.url) != repo_url:
                raise ValueError("仓库缓存的远端身份不一致。")
        return local_path

    staging = Path(tempfile.mkdtemp(prefix=".clone-", dir=CLONE_DIR))
    try:
        target = staging/"repo"
        run_git(["clone", "--no-checkout", "--depth=1", "--no-tags", "--", repo_url, str(target.resolve())],
                cwd=str(staging), size_path=staging)
        target.rename(local_path)
    finally:
        shutil.rmtree(staging)
    return local_path


def remove_repo(repo_url: str) -> None:
    """Remove only the canonical repository's clone, never an arbitrary path."""
    from indexer.snapshots import normalize_repo_url
    url = normalize_repo_url(repo_url)
    owner, name = url.removeprefix("https://github.com/").split("/")
    root = Path(CLONE_DIR).resolve()
    target = root / f"{owner}__{name}"
    if target.is_symlink():
        raise ValueError("仓库缓存不能是符号链接。")
    if target.exists():
        shutil.rmtree(target)
