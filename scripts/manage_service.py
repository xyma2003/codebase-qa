import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.service_control import counts, resume, wait_idle

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("status", "drain", "resume"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=float, default=330)
    args = parser.parse_args()
    if not 0 <= args.timeout <= 600:
        parser.error("timeout must be 0..600")
    directory = args.root / ".baseline_runtime/tasks"
    try:
        if args.operation == "drain":
            result = wait_idle(directory, args.timeout)
        else:
            if args.operation == "resume":
                resume(directory)
            result = counts(directory)
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        sys.exit(1)
