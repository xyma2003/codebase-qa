"""Run only the module-chunk comparison against an existing successful index."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.retrieval import compare_module_chunks
from evaluation.dataset import read_json


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/codebase-qa-v1")
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--indexes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-map", type=Path)
    args = parser.parse_args()
    report = compare_module_chunks(args.dataset, args.repository, args.indexes, args.output,
                                   candidate_collections=read_json(args.candidate_map.read_bytes()) if args.candidate_map else None)
    print(json.dumps({"status": "retrieval_comparison_completed", "real_model_requests": 0,
                      "default_changed": False, "summaries": {name: value["summary"] for name, value in report["variants"].items()}}))
