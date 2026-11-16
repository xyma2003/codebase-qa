from dataclasses import dataclass, fields
from pathlib import Path
import os
import tomllib

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TaskLimits:
    running_answers: int = 2
    running_imports: int = 1
    running_per_user: int = 1
    queued_total: int = 20
    queued_per_user: int = 2
    daily_answers_per_user: int = 20
    daily_imports_per_user: int = 5
    daily_answers_total: int = 100
    daily_imports_total: int = 20
    import_timeout_seconds: int = 600
    retained_days: int = 7
    max_records: int = 1000
    storage_bytes: int = 128 * 1024 * 1024
    result_bytes: int = 4 * 1024 * 1024

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not int or getattr(self, f.name) <= 0 for f in fields(self)):
            raise ValueError("任务限制必须为正整数。")
        if self.result_bytes > self.storage_bytes:
            raise ValueError("任务结果上限超过存储预算。")


def load_limits(path=None):
    path = Path(path or os.environ.get("CODEBASE_QA_TASK_LIMITS_FILE", ROOT / ".streamlit/tasks.toml"))
    if not path.exists() and not path.is_symlink() and path == ROOT / ".streamlit/tasks.toml":
        return TaskLimits()
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError
        return TaskLimits(**tomllib.loads(path.read_text()))
    except (OSError, TypeError, ValueError):
        raise ValueError("任务配置无效。") from None
