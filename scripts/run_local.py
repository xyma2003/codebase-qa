"""Explicit single-operator localhost launcher; never use behind a public proxy."""

import argparse
import os
from pathlib import Path
import sys

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1–65535")
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    os.environ["CODEBASE_QA_LOCAL_MODE"] = "1"
    os.execv(sys.executable,[sys.executable,"-m","streamlit","run",str(root/"app.py"),
                            "--server.address","127.0.0.1","--server.port",str(args.port),"--server.headless","true"])
