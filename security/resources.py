"""Server limits; model output and custom run settings cannot raise these caps."""

MAX_SOURCE_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 10 * MAX_SOURCE_BYTES
MAX_SNAPSHOT_FILES = 2000
MAX_GIT_BYTES = 256 * MAX_SOURCE_BYTES
MAX_GIT_SECONDS = 120
MAX_LOG_BYTES = MAX_SOURCE_BYTES
MAX_REQUESTS = 50
MAX_TOOL_CALLS = 50
MAX_RUN_SECONDS = 300
MAX_INDEX_CHUNKS = 10000


def default_run_limits():
    return {"request_limit": MAX_REQUESTS, "tool_calls_limit": MAX_TOOL_CALLS,
            "timeout_seconds": MAX_RUN_SECONDS, "input_bytes": MAX_SOURCE_BYTES,
            "output_bytes": MAX_SOURCE_BYTES}


def snapshot_budget(files):
    if not isinstance(files, dict) or any(not isinstance(meta, dict) or type(meta.get("bytes")) is not int or meta["bytes"] < 0
                                         or type(meta.get("materialized", True)) is not bool for meta in files.values()):
        raise ValueError("快照文件清单无效。")
    if len(files) > MAX_SNAPSHOT_FILES:
        raise ValueError("仓库文件数超过安全上限。")
    total = sum(meta["bytes"] for meta in files.values() if meta.get("materialized", True) and meta["bytes"] <= MAX_SOURCE_BYTES)
    if total > MAX_SNAPSHOT_BYTES:
        raise ValueError("源码快照总大小超过安全上限。")


def check_bytes(value, label):
    if len(value.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError(f"{label}超过安全字节上限。")
