"""Remove unnecessary privilege paths; this is not a util-linux source patch."""
import argparse
import json
import os
from pathlib import Path
import stat

BLOCKED = ("mount", "umount", "nsenter", "unshare")


def harden(root=Path("/")):
    root = Path(root)
    changed = []
    # Debian uses merged /usr. Do not follow symlinks into mounted data.
    for directory, _, files in os.walk(root / "usr", followlinks=False):
        for name in files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode) and mode & 0o6000:
                path.chmod(stat.S_IMODE(mode) & ~0o6000)
                changed.append(str(path.relative_to(root)))
    for name in BLOCKED:
        path = root / "usr/bin" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Required privilege tool missing or redirected: " + name)
        path.chmod(0)
    return {"removed_setid": sorted(changed), "blocked_tools": list(BLOCKED), "upstream_patched": False}


def verify(root=Path("/"), *, runtime=False):
    root = Path(root)
    tools = {}
    for name in BLOCKED:
        path = root / "usr/bin" / name
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0:
            raise ValueError("Privilege tool is accessible: " + name)
        if runtime and (path.stat().st_uid != 0 or os.access(path, os.R_OK | os.X_OK)):
            raise ValueError("Privilege tool owner or permissions changed: " + name)
        tools[name] = {"mode": "000", "owner": path.stat().st_uid}
    for directory, _, files in os.walk(root / "usr", followlinks=False):
        for name in files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode) and mode & 0o6000:
                raise ValueError("Set-ID executable remains: " + str(path))
    if runtime:
        fields = dict(line.split(":", 1) for line in (root / "proc/self/status").read_text().splitlines() if ":" in line)
        if os.getuid() != 10001 or fields.get("NoNewPrivs", "").strip() != "1":
            raise ValueError("Expected non-root and no-new-privileges")
        if any(int(fields.get(key, "1").strip(), 16) for key in ("CapEff", "CapPrm", "CapBnd", "CapAmb")):
            raise ValueError("Container capabilities remain")
    return {"blocked_tools": tools, "setid_files": 0, "runtime_checked": runtime, "upstream_patched": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--harden", action="store_true")
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args()
    report = harden() if args.harden else verify(runtime=args.runtime)
    print(json.dumps(report, sort_keys=True))
