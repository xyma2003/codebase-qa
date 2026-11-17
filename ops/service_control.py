"""Trusted-host admission drain. No questions or answers are exposed."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import time


@contextmanager
def admission(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / "admission.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def begin_drain(root):
    root = Path(root)
    with admission(root):
        path = root / "draining"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)


def resume(root):
    root = Path(root)
    with admission(root):
        path = root / "draining"
        if path.is_symlink():
            raise ValueError("维护标记无效。")
        path.unlink(missing_ok=True)


def counts(root):
    root = Path(root)
    path = root / "tasks.sqlite3"
    result = {"queued": 0, "running": 0, "cancelling": 0, "draining": (root / "draining").exists()}
    if not path.exists():
        return result
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    try:
        for state, count in db.execute("SELECT state,COUNT(*) FROM tasks WHERE state IN ('queued','running','cancelling') GROUP BY state"):
            result[state] = count
    finally:
        db.close()
    return result


def wait_idle(root, timeout):
    begin_drain(root)
    deadline = time.monotonic() + timeout
    while True:
        result = counts(root)
        if sum(result[state] for state in ("queued", "running", "cancelling")) == 0:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError("任务尚未结束；保持停止接单，不自动重跑或恢复。")
        time.sleep(.2)
