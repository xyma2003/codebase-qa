import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.dependency_audit import audit, check_installed, groups

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=ROOT / "dependencies/runtime-macos.lock")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-installed", action="store_true")
    parser.add_argument("--collect-only", action="store_true", help="只收集公告；不表示发布审核通过")
    args = parser.parse_args()
    if args.check_installed:
        differences = check_installed(args.lock)
        if differences:
            print(json.dumps({"status": "installed_mismatch", "differences": differences}))
            sys.exit(2)
    result = audit(args.lock, args.output)
    print(json.dumps({"complete": result["complete"], "packages": len(result["packages"]),
                      "advisory_ids": len(result["findings"]), "advisory_groups": len(groups(result["findings"])),
                      "scope": "python_dependencies_only",
                      "python_dependency_gate_clear": result["complete"] and not result["findings"], "error": result["error"]}, ensure_ascii=False))
    sys.exit(0 if result["complete"] and (args.collect_only or not result["findings"]) else 1)
