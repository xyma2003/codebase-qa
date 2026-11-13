"""Operator-registered archives and per-user review storage for web admins."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid4

from security.identity import AccessDenied, Principal, require_admin
from security.sources import ensure_safe_text

STATE_ROOT = Path(__file__).resolve().parents[1]/".baseline_runtime/reviews"


def _catalog():
    path = STATE_ROOT/"catalog.json"
    if not path.exists():
        return []
    if path.is_symlink():
        raise AccessDenied("运行记录登记无效。")
    with path.open("rb") as file:
        raw = file.read(1024*1024+1)
    if len(raw) > 1024*1024:
        raise AccessDenied("运行记录登记超过上限。")
    data = json.loads(raw)
    if data.get("format_version") != 1 or not isinstance(data.get("runs"), list):
        raise AccessDenied("运行记录登记无效。")
    return data["runs"]


def register_review_run(archive, owner_id, name):
    if require_admin().owner_id != "local":
        raise AccessDenied("仅本地操作员可登记运行记录。")
    Principal(owner_id, "admin")
    if not name.strip() or len(name) > 100:
        raise ValueError("运行名称应为 1–100 个字符。")
    ensure_safe_text(name)
    source = Path(archive).resolve(strict=True)
    seal = source/"archive.json"
    if not source.is_dir() or seal.is_symlink() or not seal.is_file():
        raise ValueError("请登记已有封存运行。")
    row = {"id":str(uuid4()),"owner_id":owner_id,"name":name.strip(),"archive":str(source),
           "seal_sha256":hashlib.sha256(seal.read_bytes()).hexdigest()}
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(STATE_ROOT/"catalog.lock", os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor,"r+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        rows = _catalog()
        if len(rows) >= 1000:
            raise ValueError("登记运行数量达到上限。")
        raw = json.dumps({"format_version":1,"runs":[*rows,row]},ensure_ascii=False,indent=2)+"\n"
        if len(raw.encode()) > 1024*1024:
            raise ValueError("运行记录登记超过上限。")
        fd, temporary = tempfile.mkstemp(prefix=".catalog-",dir=STATE_ROOT)
        try:
            with os.fdopen(fd,"w") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary,STATE_ROOT/"catalog.json")
        finally:
            Path(temporary).unlink(missing_ok=True)
    return row["id"]


def list_review_runs():
    owner = require_admin().owner_id
    return [row for row in _catalog() if row["owner_id"] == owner]


def resolve_review_run(identity):
    try:
        if str(UUID(identity)) != identity:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise AccessDenied("运行记录不存在或没有访问权限。") from None
    row = next((row for row in list_review_runs() if row["id"] == identity),None)
    if row is None:
        raise AccessDenied("运行记录不存在或没有访问权限。")
    path = Path(row["archive"])
    seal = path/"archive.json"
    if path.is_symlink() or seal.is_symlink() or hashlib.sha256(seal.read_bytes()).hexdigest() != row["seal_sha256"]:
        raise AccessDenied("运行归档已经变化，请重新登记。")
    return path.resolve(strict=True)


def review_directory():
    return STATE_ROOT/"users"/require_admin().owner_id


def authorize_review(archive, *, packet=None, directory=None, report=None):
    actor = require_admin()
    if actor.owner_id == "local":
        return
    resolved = Path(archive).resolve()
    if not any(resolve_review_run(row["id"]) == resolved for row in list_review_runs()):
        raise AccessDenied("运行记录不存在或没有访问权限。")
    root = review_directory().resolve()
    if directory is not None and Path(directory).resolve() != root:
        raise AccessDenied("不能访问其他用户的评审目录。")
    for value, category in ((packet,"packets"),(report,"reports")):
        if value is None:
            continue
        path = Path(value)
        resolved_path = path.resolve()
        if (resolved_path.parent != root/category or path.is_symlink()
                or path.parent.is_symlink() or review_directory().is_symlink()):
            raise AccessDenied("评审记录不存在或没有访问权限。")
