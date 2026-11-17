"""A single-process, bounded dispatcher with a durable private task ledger."""
import fcntl
import logging
from pathlib import Path
from threading import Event, RLock, Thread
import time

from security.identity import as_principal, current_principal
from security.output import safe_error
from security.sources import ensure_safe_text
from tasks.context import TaskCancelled, TaskControl, task_context
from tasks.limits import TaskLimits, load_limits
from tasks.store import TaskRejected, TaskStore, LIVE, encode
from ops.service_control import admission

ROOT = Path(__file__).resolve().parents[1]


class TaskService:
    def __init__(self, root, *, limits=None, handlers=None, start=True):
        self.limits = limits or TaskLimits()
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lease = (root / "worker.lock").open("a+")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lease.close()
            raise TaskRejected("任务队列已被其他进程使用；当前仅支持单服务进程。") from None
        try:
            self.store = TaskStore(root, self.limits)
            self.store.recover()
        except BaseException:
            self._lease.close()
            raise
        if handlers is None:
            from tasks.handlers import answer, import_repo
            handlers = {"answer": answer, "import": import_repo}
        self._handlers = handlers
        self._lock = RLock()
        self._wake = Event()
        self._closed = False
        self._broken = False
        self._principals = {}
        self._running = {}
        self._last_owner = None
        self._dispatcher = None
        if start:
            self._dispatcher = Thread(target=self._serve, name="codebase-qa-task-dispatch", daemon=True)
            self._dispatcher.start()

    def _available(self):
        if self._closed or self._broken:
            raise TaskRejected("任务队列暂不可用。")

    def submit(self, kind, payload, submission):
        principal = current_principal()
        # All normalization and source/conversation authorization happens before
        # admission. Never accept a path, owner or a callable from the browser.
        from tasks.handlers import normalize
        normalized, scope = normalize(kind, payload)
        ensure_safe_text(encode(normalized))
        with self._lock:
            self._available()
            with admission(self.store.root):
                if (self.store.root / "draining").exists():
                    raise TaskRejected("服务维护中，暂不接收新任务。")
                task_id, created = self.store.admit(principal.owner_id, submission, kind, scope, normalized)
            if created:
                self._principals[task_id] = principal
            self._wake.set()
            return task_id

    def get(self, task_id):
        principal = current_principal()
        self._available()
        return self.store.read(principal.owner_id, task_id)

    def list(self):
        principal = current_principal()
        self._available()
        return self.store.list_owned(principal.owner_id)

    def cancel(self, task_id):
        principal = current_principal()
        with self._lock:
            self._available()
            task = self.store.read(principal.owner_id, task_id)
            if task["state"] not in LIVE:
                return task["state"]
            control = self._running.get(task_id, {}).get("control")
            if control is None:
                self.store.state(task_id, "cancelled", error="任务已取消。")
                self._principals.pop(task_id, None)
                return "cancelled"
            with control.lock:
                if control.published:
                    raise TaskRejected("任务结果已保存，请等待状态更新。")
                control.cancelled.set()
                self.store.state(task_id, "cancelling")
                return "cancelling"

    def _serve(self):
        while not self._closed:
            self._wake.wait(.2)
            self._wake.clear()
            try:
                self.step()
            except Exception as exc:
                # Fail closed on ledger/dispatcher failure. Do not silently
                # requeue a request that might already have reached a provider.
                if self._closed:
                    return
                self._broken = True
                logging.getLogger(__name__).error("Task dispatcher stopped: %s", safe_error(exc))
                return

    def step(self):
        """Claim under one lock; quotas were already reserved in SQLite."""
        with self._lock:
            self._available()
            for task_id, running in self._running.items():
                if running["kind"] == "import" and time.monotonic() - running["started"] >= self.limits.import_timeout_seconds:
                    control = running["control"]
                    with control.lock:
                        if not control.published and not control.cancelled.is_set():
                            control.failure = "导入任务超过服务端时限。"
                            control.cancelled.set()
                            self.store.state(task_id, "cancelling")
            pending = self.store.queued()
            owners = list(dict.fromkeys(task["owner"] for task in pending))
            if self._last_owner in owners:
                index = owners.index(self._last_owner) + 1
                owners = owners[index:] + owners[:index]
            for owner in owners:
                for task in (item for item in pending if item["owner"] == owner):
                    running = list(self._running.values())
                    cap = self.limits.running_answers if task["kind"] == "answer" else self.limits.running_imports
                    if (sum(row["kind"] == task["kind"] for row in running) >= cap or
                            sum(row["owner"] == owner for row in running) >= self.limits.running_per_user or
                            any(row["scope"] == task["scope"] for row in running)):
                        continue
                    principal = self._principals.get(task["id"])
                    if principal is None:
                        self.store.state(task["id"], "interrupted", error="提交身份不可恢复，任务已中断。")
                        continue
                    control = TaskControl()
                    thread = Thread(target=self._execute, args=(task, principal, control),
                                    name="codebase-qa-task-" + task["id"][:8], daemon=True)
                    self._running[task["id"]] = dict(kind=task["kind"], owner=owner, scope=task["scope"], control=control,
                                                    thread=thread, started=time.monotonic())
                    self.store.state(task["id"], "running")
                    self._last_owner = owner
                    thread.start()

    def _execute(self, task, principal, control):
        try:
            with as_principal(principal), task_context(control):
                control.check()
                def progress(value):
                    control.check()
                    self.store.progress(task["id"], value)
                result = self._handlers[task["kind"]](task["payload"], progress)
                with control.lock:
                    control.check()
                    ensure_safe_text(encode(result))
                    self.store.state(task["id"], "succeeded", result=result)
                    control.published = True
        except BaseException as exc:
            if isinstance(exc, TaskCancelled):
                self.store.state(task["id"], "failed" if control.failure else "cancelled", error=str(exc))
            else:
                # Keep the original redacted error; no retry or provider fallback.
                self.store.state(task["id"], "failed", error=safe_error(exc)[:2000])
        finally:
            with self._lock:
                self._running.pop(task["id"], None)
                self._principals.pop(task["id"], None)
                self._wake.set()

    def wait(self, task_id, seconds=.2):
        """Only a short UI fast path; background work survives the caller."""
        deadline = time.monotonic() + seconds
        while True:
            task = self.get(task_id)
            if task["state"] not in LIVE or time.monotonic() >= deadline:
                return task
            self._wake.set()
            time.sleep(.01)

    def close(self, timeout=5):
        with self._lock:
            self._closed = True
            self._wake.set()
            threads = [row["thread"] for row in self._running.values()]
            for row in self._running.values():
                with row["control"].lock:
                    if not row["control"].published:
                        row["control"].cancelled.set()
        if self._dispatcher:
            self._dispatcher.join(timeout)
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        with self._lock:
            if self._running:
                raise TaskRejected("后台工作尚未停止，队列锁仍被保留。")
            self._lease.close()


_singleton_lock = RLock()
_singleton = None


def task_service():
    global _singleton
    current_principal()
    with _singleton_lock:
        if _singleton is None:
            _singleton = TaskService(ROOT / ".baseline_runtime/tasks", limits=load_limits())
        return _singleton
