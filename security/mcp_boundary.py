"""Checks run outside the MCP client's model, for every registered tool."""

from functools import wraps
import json

from security.output import redact_data, safe_error
from security.resources import check_bytes
from security.sources import ensure_safe_text


def mcp_boundary(function):
    @wraps(function)
    def checked(*args, **kwargs):
        arguments = json.dumps([args, kwargs], ensure_ascii=False)
        check_bytes(arguments, "工具参数")
        ensure_safe_text(arguments)
        try:
            result = function(*args, **kwargs)
            check_bytes(json.dumps(result, ensure_ascii=False), "工具结果")
            return redact_data(result)
        except Exception as exc:
            raise ValueError(safe_error(exc)) from None
    return checked
