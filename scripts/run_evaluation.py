"""Run only the local fixture backend; never start a remote model batch."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.runner import run_local


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/codebase-qa-v1")
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--backend", choices=["local"], default="local")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-local-" + uuid4().hex[:8]
    destination = args.output or ROOT / ".baseline_runtime/evaluations" / run_id
    result = asyncio.run(run_local(args.dataset, args.repository, destination))
    print(json.dumps({"output": str(destination.resolve()), **result}, ensure_ascii=False))
    raise SystemExit(1 if result["failed_turns"] else 0)
