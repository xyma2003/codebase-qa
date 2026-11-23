"""Stopped-service backups. Restore to a new directory; preserve runtime paths."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from security.sqlite import check_integrity

ROOTS = ("chroma_db", "repos", ".baseline_runtime/tasks", ".baseline_runtime/reviews", ".baseline_runtime/evaluations")
MAX_BYTES = 10 * 1024 ** 3
MAX_FILES = 200000


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        while block := file.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def allowed(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and ".." not in path.parts and "\\" not in name and ":" not in name
            and any(name.startswith(root + "/") for root in ROOTS))


@contextmanager
def maintenance(root):
    root = Path(root).absolute()
    directory = root / ".baseline_runtime/tasks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handles = []
    try:
        for name in ("maintenance.lock", "worker.lock"):
            path = directory / name
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handles.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("服务或其他运维任务仍在运行。") from None
        yield
    finally:
        for fd in handles:
            os.close(fd)


def sqlite_check(path):
    if Path(path).suffix not in {".sqlite3", ".sqlite"}:
        return
    check_integrity(path)


def check_registered_archives(root):
    catalog = root / ".baseline_runtime/reviews/catalog.json"
    if not catalog.exists():
        return
    data = json.loads(catalog.read_text())
    # Registry format is server-owned; reject unknown shape rather than omit it.
    if data.get("format_version") != 1 or not isinstance(data.get("runs"), list):
        raise ValueError("评审登记格式不受支持。")
    for row in data["runs"]:
        path = Path(row["archive"]).absolute()
        if not path.is_relative_to(root) or not allowed(path.relative_to(root).as_posix() + "/sealed"):
            raise ValueError("登记的评测归档不在备份范围内，请先迁入 evaluations 并重新登记。")


def backup(root, destination, *, service_stopped=False):
    if not service_stopped:
        raise ValueError("必须先停止服务及本机 CLI 写入，再执行备份。")
    root, destination = Path(root).absolute(), Path(destination).absolute()
    if destination.exists() or any(destination.is_relative_to(root / part) for part in ROOTS):
        raise ValueError("备份目标必须是数据目录外的新文件。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    with maintenance(root):
        check_registered_archives(root)
        records, total = {}, 0
        for part in ROOTS:
            source = root / part
            if source.is_symlink():
                raise ValueError("数据目录不能是符号链接。")
            if not source.exists():
                continue
            for path in sorted(source.rglob("*")):
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("备份不能包含符号链接或特殊文件。")
                name = path.relative_to(root).as_posix()
                total += info.st_size
                if total > MAX_BYTES or len(records) >= MAX_FILES:
                    raise ValueError("备份超过文件数或大小上限。")
                sqlite_check(path)
                records[name] = {"bytes": info.st_size}
        try:
            fd, temporary = tempfile.mkstemp(prefix=".backup-", dir=destination.parent)
            os.close(fd)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, record in records.items():
                    source = root / name
                    before = source.stat()
                    digest = hashlib.sha256()
                    with source.open("rb") as file, archive.open(name, "w") as target:
                        while block := file.read(1024 * 1024):
                            digest.update(block)
                            target.write(block)
                    after = source.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                        raise ValueError("备份期间文件被修改。")
                    record["sha256"] = digest.hexdigest()
                manifest = {"format": 1, "runtime_root": str(root), "created_at": datetime.now(timezone.utc).isoformat(), "files": records}
                archive.writestr("backup.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            # No overwrite if another operator created the requested name.
            os.link(temporary, destination)
            return {"files": len(records), "bytes": total, "sha256": digest_file(destination)}
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)


def restore(archive_path, destination, *, runtime_root):
    destination = Path(destination).absolute()
    if destination.exists():
        raise ValueError("恢复只允许新的空目录，不覆盖现有数据。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
    reserved = False
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if (len(entries) > MAX_FILES + 1 or len({item.filename for item in entries}) != len(entries)
                    or sum(item.file_size for item in entries) > MAX_BYTES + 8 * 1024 * 1024):
                raise ValueError("备份文件清单重复或超出上限。")
            if archive.getinfo("backup.json").file_size > 8 * 1024 * 1024:
                raise ValueError("备份清单过大。")
            manifest = json.loads(archive.read("backup.json"))
            if manifest.get("format") != 1 or manifest.get("runtime_root") != str(Path(runtime_root).absolute()):
                raise ValueError("备份运行根目录不一致；不能静默改写绝对路径。")
            records = manifest["files"]
            if not isinstance(records, dict) or set(records) | {"backup.json"} != {item.filename for item in entries}:
                raise ValueError("备份内容与清单不一致。")
            for info in entries:
                if info.filename == "backup.json":
                    continue
                mode = info.external_attr >> 16
                if not allowed(info.filename) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    raise ValueError("备份包含越界路径或特殊文件。")
                record = records[info.filename]
                if type(record.get("bytes")) is not int or record["bytes"] != info.file_size:
                    raise ValueError("备份文件大小不符。")
                target = staging / info.filename
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                with archive.open(info) as source, target.open("xb") as file:
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                        file.write(block)
                target.chmod(0o600)
                if digest.hexdigest() != record["sha256"]:
                    raise ValueError("备份文件摘要不符。")
                sqlite_check(target)
        # Reserve the destination atomically. It was not present before this
        # operation and is private to this operator; never replace an old tree.
        destination.mkdir(mode=0o700)
        reserved = True
        for child in staging.iterdir():
            os.rename(child, destination / child.name)
        staging.rmdir()
        return {"files": len(records), "runtime_root": manifest["runtime_root"]}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if reserved:
            shutil.rmtree(destination, ignore_errors=True)
        raise
