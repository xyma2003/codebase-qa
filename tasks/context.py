"""Cancellation is independent of the UI and checked before private writes."""
from contextvars import ContextVar
from contextlib import contextmanager
from threading import Event, RLock

from security.identity import current_principal

_control = ContextVar("codebase_qa_task_control", default=None)


class TaskCancelled(Exception):
    pass


class TaskControl:
    def __init__(self):
        self.cancelled = Event()
        self.lock = RLock()
        self.published = False
        self.failure = None

    def check(self):
        current_principal()
        if self.cancelled.is_set():
            raise TaskCancelled(self.failure or "任务已取消。")


def check_task():
    current_principal()
    control = _control.get()
    if control is not None:
        control.check()


@contextmanager
def task_context(control):
    token = _control.set(control)
    try:
        yield
    finally:
        _control.reset(token)


@contextmanager
def publication():
    """Serialize cancellation acceptance with answer/index publication."""
    control = _control.get()
    if control is None:
        current_principal()
        yield
    else:
        with control.lock:
            control.check()
            yield
            control.published = True
