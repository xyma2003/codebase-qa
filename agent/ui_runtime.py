"""One UI model loop; identities cross by ContextVar, UI events by queue."""

import asyncio
from contextlib import suppress
from queue import Empty, Queue
from threading import Event, Lock, Thread

_lock = Lock()
_loop = None


def _serve(loop, ready):
    asyncio.set_event_loop(loop)
    ready.set()
    loop.run_forever()


def _ui_loop():
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            ready = Event()
            Thread(target=_serve,args=(_loop,ready),name="codebase-qa-ui-model",daemon=True).start()
            ready.wait()
        return _loop


def run_ui_stream(factory, *, on_event=None):
    """Run model work on its stable loop, render events on the calling UI thread.

    run_coroutine_threadsafe copies the caller's context when scheduling the
    task. No Streamlit ScriptRunContext is installed on the shared loop thread.
    """
    events = Queue()
    async def collect():
        return "".join([part async for part in factory(events.put)])
    future = asyncio.run_coroutine_threadsafe(collect(),_ui_loop())
    try:
        while not future.done():
            try:
                event = events.get(timeout=.05)
            except Empty:
                continue
            if on_event is not None:
                on_event(event)
        while True:
            try:
                event = events.get_nowait()
            except Empty:
                break
            if on_event is not None:
                on_event(event)
        return future.result()
    finally:
        if not future.done():
            future.cancel()


def run_background(factory, check_cancelled):
    """Keep a worker slot until async cancellation and cleanup have finished."""
    async def supervise():
        work = asyncio.create_task(factory())
        try:
            while not work.done():
                check_cancelled()
                await asyncio.sleep(.05)
            check_cancelled()
            return await work
        finally:
            if not work.done():
                work.cancel()
            with suppress(asyncio.CancelledError):
                await work
    return asyncio.run_coroutine_threadsafe(supervise(), _ui_loop()).result()
