import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.raw.read_text())
    if raw.get("Metadata", {}).get("ImageID") != args.image_id or not raw.get("Results"):
        raise ValueError("Trivy 未返回完整镜像扫描结果。")
    count = sum(len(row.get("Vulnerabilities", [])) for row in raw["Results"])
    report = {"complete": True, "source": "Trivy 0.76.2", "queried_at": datetime.now(timezone.utc).isoformat(),
        "image_id": args.image_id, "revision": args.revision, "lock_sha256": hashlib.sha256(args.lock.read_bytes()).hexdigest(),
        "vulnerability_records": count, "release_allowed": count == 0, "raw": raw}
    with args.output.open("x") as file:
        file.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"complete": True, "os_vulnerability_records": count, "release_allowed": count == 0}))
