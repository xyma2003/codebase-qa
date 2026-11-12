"""Local review editing with source checks and optimistic concurrency control."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4
import zipfile

from evaluation.dataset import Review, digest
from evaluation.reporting import import_review, verify_report
from evaluation.reviews import load_packet, load_source, prepare_review, review_context

PACKET_FILES = ("review.json", "reviews.jsonl", "context.json", "rubric.json")


@dataclass(frozen=True)
class ReviewWorkspace:
    archive: Path
    repository: Path
    packet: Path
    source: tuple
    header: object
    rows: list
    context: dict
    revision: str

    @property
    def bundle(self):
        return self.source[2]


def _editable_packet(packet, archive):
    packet = Path(packet)
    if packet.is_symlink() or any((packet / name).is_symlink() for name in PACKET_FILES):
        raise ValueError("评审包不能使用符号链接")
    packet = packet.resolve(strict=True)
    if packet.is_relative_to(Path(archive).resolve()):
        raise ValueError("不能编辑封存的运行归档")
    if any((parent / name).exists() for parent in (packet, *packet.parents)
           for name in ("archive.json", "report-seal.json")):
        raise ValueError("不能编辑封存的运行或报告，请打开独立评审包")
    return packet


@contextmanager
def _lock(packet):
    # flock also protects separate Streamlit sessions/processes on this machine.
    descriptor = os.open(packet / ".edit.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def _revision(packet):
    return digest(b"".join(name.encode() + b"\0" + (packet / name).read_bytes() for name in PACKET_FILES))


def _load(archive, repository, packet):
    before = _revision(packet)
    source = load_source(archive, repository)
    header, rows = load_packet(packet, source)
    if before != _revision(packet):
        raise ValueError("评审包已变化，请重新打开")
    return ReviewWorkspace(Path(archive).resolve(), Path(repository).resolve(), packet,
                           source, header, rows, review_context(source[0], source[2]), before)


def open_workspace(archive, repository, packet):
    packet = _editable_packet(packet, archive)
    with _lock(packet):
        return _load(archive, repository, packet)


def new_workspace(archive, repository, directory, *, purpose="human"):
    packet = Path(directory) / "packets" / str(uuid4())
    prepare_review(archive, repository, packet, purpose=purpose)
    return open_workspace(archive, repository, packet)


def save_grade(workspace, case_id, turn_id, *, ratings=None, fatal_errors=(), reviewer=None,
               rationale="", clear=False):
    """Write only the selected turn, refusing stale forms or incomplete scores."""
    packet = _editable_packet(workspace.packet, workspace.archive)
    with _lock(packet):
        current = _load(workspace.archive, workspace.repository, packet)
        if current.revision != workspace.revision:
            raise ValueError("评审记录已被更新，请重新打开后再保存")
        matches = [row for row in current.rows if (row.case_id, row.turn_id) == (case_id, turn_id)]
        if len(matches) != 1:
            raise ValueError("题目或轮次不存在")
        original = matches[0]
        if original.status not in {"pending_review", "reviewed"}:
            raise ValueError("这轮没有完成输出，不能评分")
        if len(set(fatal_errors)) != len(fatal_errors):
            raise ValueError("致命错误标记不能重复")
        values = dict(original.model_dump(), status="pending_review" if clear else "reviewed",
                      ratings=None if clear else ratings, fatal_errors=[] if clear else list(fatal_errors),
                      reviewer=None if clear else (reviewer or "").strip(),
                      rationale="" if clear else rationale.strip())
        updated = Review.model_validate(values)
        if any(code not in current.bundle.rubric.fatal_errors for code in updated.fatal_errors):
            raise ValueError("未知的致命错误标记")
        rows = [updated if row == original else row for row in current.rows]
        data = "".join(json.dumps(row.model_dump(), ensure_ascii=False) + "\n" for row in rows)
        descriptor, name = tempfile.mkstemp(prefix=".reviews-", dir=packet)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            if _revision(packet) != current.revision:
                raise ValueError("评审包已变化，请重新打开")
            os.replace(name, packet / "reviews.jsonl")
        finally:
            Path(name).unlink(missing_ok=True)
    return open_workspace(current.archive, current.repository, packet)


def create_report(workspace, directory):
    packet = _editable_packet(workspace.packet, workspace.archive)
    with _lock(packet):
        current = _load(workspace.archive, workspace.repository, packet)
        if current.revision != workspace.revision:
            raise ValueError("评审记录已被更新，请重新打开后再生成报告")
        destination = Path(directory) / "reports" / str(uuid4())
        receipt = import_review(current.archive, current.repository, packet, destination)
    return destination, receipt


def report_package(report, archive, repository):
    """Download the sealed report; the separate source archive is still required."""
    verify_report(report, archive, repository)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in sorted(Path(report).rglob("*")):
            if path.is_file():
                package.writestr(path.relative_to(report).as_posix(), path.read_bytes())
    return buffer.getvalue()
