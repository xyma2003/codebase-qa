import argparse
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.image_comparison import compare

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((ROOT/"deploy/runtime-layout.json").read_text())
    report = compare(json.loads(args.baseline.read_bytes()), json.loads(args.candidate.read_bytes()), manifest["removed_runtime_packages"])
    report["baseline_raw_sha256"] = hashlib.sha256(args.baseline.read_bytes()).hexdigest()
    report["candidate_raw_sha256"] = hashlib.sha256(args.candidate.read_bytes()).hexdigest()
    with args.output.open("x") as file:
        file.write(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report))
