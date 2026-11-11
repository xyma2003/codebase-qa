"""Prepare/import declared review records; never call a model or rewrite a run."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.reviews import prepare_review
from evaluation.reporting import import_review, verify_report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "import", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--repository", type=Path, default=ROOT)
        if name in {"prepare", "import"}:
            command.add_argument("--output", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--purpose", choices=["human", "simulation"], default="human")
        elif name == "import":
            command.add_argument("--packet", type=Path, required=True)
        else:
            command.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_review(args.archive, args.repository, args.output, purpose=args.purpose)
    elif args.command == "import":
        result = import_review(args.archive, args.repository, args.packet, args.output)
    else:
        result = verify_report(args.report, args.archive, args.repository)
    print(json.dumps(result, ensure_ascii=False))
