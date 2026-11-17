import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.dependency_audit import pins

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("lock", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = pins(ROOT / "docs/baseline/runs/20261110-stage0/requirements-resolved.txt")
    actual = pins(args.lock)
    changes = {name: {"original": value, "linux": actual.get(name)} for name, value in original.items()
               if actual.get(name, "").split("+")[0] != value}
    if changes or "+cpu" not in actual.get("torch", ""):
        raise ValueError("Linux 锁改变了既有公共版本或没有使用 CPU wheel：" + json.dumps(changes))
    report = {"unchanged_public_versions": len(original), "linux_packages": len(actual), "torch": actual["torch"],
              "additional_linux_packages": {name: value for name, value in actual.items() if name not in original}}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
