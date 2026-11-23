"""Private SQLite ledger. Only TaskService is a user-facing interface."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid4

from security.sqlite import connect

LIVE = ("queued", "running", "cancelling")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


class TaskRejected(ValueError):
    pass


def identity(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise TaskRejected("任务编号无效。") from None
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class TaskStore:
    def __init__(self, root, limits):
        self.root, self.limits = Path(root), limits
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "tasks.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, submission TEXT NOT NULL,
                    kind TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, day TEXT NOT NULL, scope TEXT NOT NULL,
                    payload TEXT NOT NULL, result TEXT, progress TEXT NOT NULL DEFAULT '{}',
                    error TEXT, UNIQUE(owner, submission)
                );
                CREATE INDEX IF NOT EXISTS task_owner ON tasks(owner, created);
                CREATE INDEX IF NOT EXISTS task_day ON tasks(day, kind);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE tasks SET state='interrupted', updated=?, error=? WHERE state IN (?,?,?)",
                       (time.time(), "服务已重启，任务已中断。", *LIVE))

    def admit(self, owner, submission, kind, scope, payload):
        identity(submission)
        raw, now = encode(payload), time.time()
        if len(raw.encode()) > 1024 * 1024:
            raise TaskRejected("任务输入超过安全上限。")
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.connect() as db:
            old = db.execute("SELECT * FROM tasks WHERE owner=? AND submission=?", (owner, submission)).fetchone()
            if old:
                old_payload = json.loads(old["payload"])
                # Revision is derived from the authorized conversation, not a
                # user setting. A completed round must still be idempotent.
                comparable = {key: value for key, value in payload.items() if key != "revision"}
                previous = {key: value for key, value in old_payload.items() if key != "revision"}
                if old["kind"] != kind or previous != comparable:
                    raise TaskRejected("同一提交编号不能用于不同任务。")
                return old["id"], False
            db.execute("DELETE FROM tasks WHERE state NOT IN (?,?,?) AND created<?",
                       (*LIVE, now - self.limits.retained_days * 86400))
            if kind == "answer" and db.execute("SELECT 1 FROM tasks WHERE scope=? AND state IN (?,?,?)", (scope, *LIVE)).fetchone():
                raise TaskRejected("这个对话已有未完成任务。")
            pending = db.execute("SELECT owner FROM tasks WHERE state='queued'").fetchall()
            if len(pending) >= self.limits.queued_total or sum(row[0] == owner for row in pending) >= self.limits.queued_per_user:
                raise TaskRejected("排队任务已达上限，请稍后再试。")
            counts = db.execute("SELECT owner FROM tasks WHERE day=? AND kind=?", (day, kind)).fetchall()
            suffix = "answers" if kind == "answer" else "imports"
            if (len(counts) >= getattr(self.limits, "daily_" + suffix + "_total") or
                    sum(row[0] == owner for row in counts) >= getattr(self.limits, "daily_" + suffix + "_per_user")):
                raise TaskRejected("今日任务次数已达上限。")
            # Reserve room for each unfinished result. This bounds logical data,
            # not SQLite's physical file or the public source/index directories.
            size, rows, live = db.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))+"
                "COALESCE(length(CAST(result AS BLOB)),0)+32768),0),COUNT(*),"
                "COALESCE(SUM(state IN ('queued','running','cancelling')),0) FROM tasks").fetchone()
            if rows >= self.limits.max_records or size + len(raw.encode()) + (live + 1) * self.limits.result_bytes + 32768 > self.limits.storage_bytes:
                raise TaskRejected("任务记录空间已达上限。")
            task_id = str(uuid4())
            db.execute("INSERT INTO tasks(id,owner,submission,kind,state,created,updated,day,scope,payload) "
                       "VALUES(?,?,?,?,'queued',?,?,?,?,?)", (task_id, owner, submission, kind, now, now, day, scope, raw))
            return task_id, True

    def read(self, owner, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE owner=? AND id=?", (owner, identity(task_id))).fetchone()
        if row is None:
            raise TaskRejected("任务不存在或没有访问权限。")
        return self.unpack(row)

    @staticmethod
    def unpack(row):
        value = dict(row)
        for name in ("payload", "result", "progress"):
            value[name] = json.loads(value[name]) if value[name] is not None else None
        return value

    def list_owned(self, owner):
        with self.connect() as db:
            rows = db.execute("SELECT id,kind,state,created,updated,progress,error,"
                              "substr(COALESCE(json_extract(payload,'$.question'),json_extract(payload,'$.repo_url')),1,80) AS title FROM tasks "
                              "WHERE owner=? ORDER BY created DESC LIMIT 50", (owner,)).fetchall()
        return [dict(row, progress=json.loads(row["progress"])) for row in rows]

    def queued(self):
        with self.connect() as db:
            return [self.unpack(row) for row in db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created,id")]

    def state(self, task_id, state, *, result=None, error=None):
        raw = encode(result) if result is not None else None
        if raw is not None and len(raw.encode()) > self.limits.result_bytes:
            raise TaskRejected("任务结果超过存储上限。")
        with self.connect() as db:
            db.execute("UPDATE tasks SET state=?,updated=?,result=?,error=? WHERE id=?",
                       (state, time.time(), raw, error, task_id))

    def progress(self, task_id, value):
        raw = encode(value)
        if len(raw.encode()) > 16384:
            raise TaskRejected("任务进度超过安全上限。")
        with self.connect() as db:
            db.execute("UPDATE tasks SET progress=?,updated=? WHERE id=?", (raw, time.time(), task_id))
