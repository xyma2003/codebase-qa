"""Read back a completed archive directory, including after ZIP extraction."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.archive import verify_archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--repository", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(verify_archive(args.archive, args.repository), ensure_ascii=False))
