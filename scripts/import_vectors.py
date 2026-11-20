"""Import legacy vectors after explicit maintenance and checksum verification."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.backup import maintenance
from ops.vector_migration import import_export


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--service-stopped", action="store_true")
    args = parser.parse_args()
    with maintenance(args.destination.absolute().parent):
        print(json.dumps(import_export(args.source, args.destination, args.sha256, service_stopped=args.service_stopped)))
