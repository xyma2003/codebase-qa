from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re


def validate_os_audit(report, lock, revision, image_id, *, now=None):
    if (not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id or "") or report.get("image_id") != image_id
            or report.get("revision") != revision or report.get("complete") is not True
            or report.get("lock_sha256") != hashlib.sha256(Path(lock).read_bytes()).hexdigest()):
        raise ValueError("系统依赖审计与部署镜像不符。")
    age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(report["queried_at"])).total_seconds()
    if not -60 <= age <= 7 * 86400:
        raise ValueError("系统依赖审计已过期。")
    raw = report.get("raw", {})
    if raw.get("Metadata", {}).get("ImageID") != image_id or not isinstance(raw.get("Results"), list) or not raw["Results"]:
        raise ValueError("系统依赖审计缺少实际镜像结果。")
    if not any(row.get("Class") == "os-pkgs" for row in raw["Results"]):
        raise ValueError("系统依赖审计缺少操作系统包覆盖。")
    for result in raw["Results"]:
        if result.get("Vulnerabilities"):
            raise ValueError("系统依赖仍有未处理公告，禁止上线。")
