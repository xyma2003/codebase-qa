"""Disable unused ncurses/Perl parsers, retaining package audit visibility.

This removes two runtime entry points, not their source-package advisories.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess

IDS = ["CVE-2025-69720", "CVE-2026-9538"]


def _paths(root):
    root = Path(root)
    paths = [root / "usr/bin/infocmp"]
    # Do not follow directory symlinks or guess the installed Perl version.
    modules = []
    for directory, _, files in os.walk(root / "usr", followlinks=False):
        if Path(directory).name == "Archive" and "Tar.pm" in files:
            modules.append(Path(directory) / "Tar.pm")
    if not modules:
        raise ValueError("Expected installed Perl Archive::Tar module is missing")
    paths.extend(sorted(modules))
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Optional parser is missing or redirected: " + str(path))
    return paths


def harden(root=Path("/")):
    root = Path(root)
    paths = _paths(root)
    for path in paths:
        path.chmod(0)
    return {"disabled_files": [str(p.relative_to(root)) for p in paths], "advisories": IDS,
            "upstream_patched": False, "package_inventory_retained": True}


def verify(root=Path("/"), *, runtime=False):
    root = Path(root)
    paths = _paths(root)
    for path in paths:
        if stat.S_IMODE(path.stat().st_mode) != 0:
            raise ValueError("Optional parser permissions changed: " + str(path))
        if runtime and (path.stat().st_uid != 0 or os.access(path, os.R_OK) or os.access(path, os.X_OK)):
            raise ValueError("Optional parser is accessible: " + str(path))
    probes = {}
    if runtime:
        if root != Path("/") or os.getuid() != 10001:
            raise ValueError("Parser probes require the real non-root runtime")
        try:
            subprocess.run(["/usr/bin/infocmp", "-a"], capture_output=True, check=False, timeout=5)
        except PermissionError:
            probes["infocmp_execute_denied"] = True
        else:
            raise ValueError("infocmp execution was not denied")
        result = subprocess.run(["/usr/bin/perl", "-MArchive::Tar", "-e", "exit 0"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 or "Archive/Tar.pm" not in result.stderr or "Permission denied" not in result.stderr:
            raise ValueError("Perl Archive::Tar denial was not demonstrated: " + result.stderr)
        probes["archive_tar_import_denied"] = True
        result = subprocess.run(["/usr/bin/perl", "-MError", "-e", "print 'git_perl_ok'"], capture_output=True, text=True, timeout=5)
        if result.returncode or result.stdout != "git_perl_ok":
            raise ValueError("Git's required Perl dependency is broken: " + result.stderr)
        probes["git_perl_dependency_works"] = True
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode or not result.stdout.startswith("git version "):
            raise ValueError("Git is broken after disabling optional parsers")
        probes["git_starts"] = True
    return {"disabled_files": [str(p.relative_to(root)) for p in paths], "advisories": IDS,
            "runtime_probes": probes, "upstream_patched": False, "package_inventory_retained": True,
            "public_release_allowed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--harden", action="store_true")
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args()
    print(json.dumps(harden() if args.harden else verify(runtime=args.runtime), indent=2))
