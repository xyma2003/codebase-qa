"""Read a complete raw scan and write review records without changing a gate."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.os_triage import summarize, markdown

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.scan.is_symlink() or not 0 < args.scan.stat().st_size <= 32 * 1024 * 1024:
        parser.error("scan must be a bounded regular file")
    raw = args.scan.read_bytes()
    report = dict(summarize(json.loads(raw)), raw_sha256=hashlib.sha256(raw).hexdigest())
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (args.output / "review.md").write_text(markdown(report))
    print(json.dumps({k:v for k,v in report.items() if k != "groups"}, ensure_ascii=False))
