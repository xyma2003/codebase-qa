"""Build only the dataset's pinned versions; do not fetch or change app versions."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.indexes import build_indexes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/codebase-qa-v1")
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_indexes(args.dataset, args.repository, args.output), ensure_ascii=False))
