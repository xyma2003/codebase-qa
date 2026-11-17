"""Liveness only. Public response contains no user, queue or configuration data."""
import sys
import urllib.request

if __name__ == "__main__":
    try:
        with urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health", timeout=3) as response:
            if response.status != 200 or response.read(16).strip().lower() != b"ok":
                raise ValueError
    except Exception:
        sys.exit(1)
