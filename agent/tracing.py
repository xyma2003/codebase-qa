"""Source-tool events, without model reasoning or inferred execution claims."""

from functools import wraps
import inspect
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ToolEvent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    call_id: str
    tool_name: str
    arguments: dict
    status: Literal["running", "returned", "failed"]
    elapsed_ms: int | None = Field(default=None, ge=0)
    output_chars: int | None = Field(default=None, ge=0)


def _emit(deps, event):
    observer = getattr(deps, "tool_observer", None)
    if observer:
        try:
            observer(event)
        except Exception:
            pass  # Display errors do not invalidate source reads.


def traced_tool(function):
    @wraps(function)
    async def wrapped(ctx, *args, **kwargs):
        values = inspect.signature(function).bind(ctx, *args, **kwargs)
        values.apply_defaults()
        arguments = {name: value for name, value in values.arguments.items() if name != "ctx"}
        event = ToolEvent(call_id=str(uuid4()), tool_name=function.__name__, arguments=arguments, status="running")
        _emit(ctx.deps, event)
        started = time.perf_counter()
        try:
            result = await function(ctx, *args, **kwargs)
        except BaseException:
            completed = event.model_copy(update={"status": "failed", "elapsed_ms": round((time.perf_counter()-started)*1000)})
            getattr(ctx.deps, "tool_events", []).append(completed)
            _emit(ctx.deps, completed)
            raise
        completed = event.model_copy(update={"status": "returned", "elapsed_ms": round((time.perf_counter()-started)*1000),
                                            "output_chars": len(result) if isinstance(result, str) else None})
        getattr(ctx.deps, "tool_events", []).append(completed)
        _emit(ctx.deps, completed)
        return result
    return wrapped
