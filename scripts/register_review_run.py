"""Register a sealed evaluation for one administrator, from the trusted host."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from security.identity import user_id
from security.review_access import register_review_run

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive",type=Path)
    parser.add_argument("--name",required=True)
    parser.add_argument("--issuer")
    parser.add_argument("--subject")
    args = parser.parse_args()
    if bool(args.issuer) != bool(args.subject):
        parser.error("issuer and subject must be supplied together")
    owner = user_id(args.issuer,args.subject) if args.issuer else "local"
    print(register_review_run(args.archive,owner,args.name))
