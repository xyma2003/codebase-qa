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
    parser.add_argument("--backend", choices=["local", "siliconflow"], default="local")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--indexes", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{args.backend}-" + uuid4().hex[:8]
    destination = args.output or ROOT / ".baseline_runtime/evaluations" / run_id
    if args.backend == "siliconflow":
        if args.baseline is None or args.indexes is None:
            parser.error("真实评测必须提供 --baseline 成功基线和 --indexes 固定索引；不自动创建或修改配置")
        from evaluation.real_runner import run_real
        result = asyncio.run(run_real(args.dataset, args.repository, destination,
                                      baseline_directory=args.baseline, index_directory=args.indexes))
    else:
        if args.baseline is not None or args.indexes is not None:
            parser.error("本地源码模拟不接受真实评测参数")
        result = asyncio.run(run_local(args.dataset, args.repository, destination))
    print(json.dumps({"output": str(destination.resolve()), **result}, ensure_ascii=False))
    raise SystemExit(1 if result["failed_turns"] else 0)
