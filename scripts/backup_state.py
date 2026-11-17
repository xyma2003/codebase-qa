import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.backup import backup, restore

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    save = sub.add_parser("create")
    save.add_argument("--root", type=Path, default=ROOT)
    save.add_argument("--output", type=Path, required=True)
    save.add_argument("--service-stopped", action="store_true")
    load = sub.add_parser("restore")
    load.add_argument("archive", type=Path)
    load.add_argument("--output", type=Path, required=True)
    load.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = backup(args.root, args.output, service_stopped=args.service_stopped) if args.operation == "create" else restore(args.archive, args.output, runtime_root=args.runtime_root)
        print(json.dumps({"status": "complete", **result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__ + ": " + str(exc)[:500]}, ensure_ascii=False))
        sys.exit(1)
