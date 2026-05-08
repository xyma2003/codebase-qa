import os
import shutil
from git import Repo
from config import CLONE_DIR


def clone_repo(repo_url: str) -> str:
    """
    Clone a GitHub repo (shallow) and return the local path.
    If already cloned, return the existing path without re-cloning.
    """
    os.makedirs(CLONE_DIR, exist_ok=True)

    # Derive a folder name from the URL: owner__reponame
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    owner = repo_url.rstrip("/").split("/")[-2]
    local_path = os.path.join(CLONE_DIR, f"{owner}__{repo_name}")

    if os.path.exists(local_path):
        return local_path

    Repo.clone_from(repo_url, local_path, depth=1)
    return local_path


def remove_repo(repo_url: str) -> None:
    """Remove a previously cloned repo."""
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    owner = repo_url.rstrip("/").split("/")[-2]
    local_path = os.path.join(CLONE_DIR, f"{owner}__{repo_name}")
    if os.path.exists(local_path):
        shutil.rmtree(local_path)
