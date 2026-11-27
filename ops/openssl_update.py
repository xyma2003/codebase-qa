"""Install only the three locally backported OpenSSL runtime packages."""
import argparse
import json
from pathlib import Path

from ops.expat_update import command, inventory
from ops.openssl_build import digest


PACKAGES = {"libssl3t64", "openssl", "openssl-provider-legacy"}


def validate_changes(before, after, manifest):
    expected = {name: {"before": {"status": "installed", "version": manifest["source_version"]},
                       "after": {"status": "installed", "version": manifest["package_version"]}}
                for name in manifest["runtime_packages"]}
    actual = {name: {"before": before.get(name), "after": after.get(name)}
              for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    if actual != expected:
        raise ValueError("Unexpected package changes during OpenSSL update: " + json.dumps(actual, sort_keys=True))
    return actual


def check_build(manifest, report, directory):
    if (manifest.get("schema") != 1 or set(manifest["runtime_packages"]) != PACKAGES
            or set(report["packages"]) != PACKAGES or report["patches"] != manifest["patches"]
            or report["source_dsc_sha256"] != manifest["source_files"][0]["sha256"]
            or report["package_version"] != manifest["package_version"]
            or report["gc_control"]["baseline"]["exit_code"] != 1
            or report["gc_control"]["patched"]["exit_code"] != 0):
        raise ValueError("OpenSSL packages are not bound to the verified backport")
    if report.get("debian_signature_required") is not True or not report.get("quic_regression_lines"):
        raise ValueError("OpenSSL source signature or QUIC regression evidence is missing")
    totals = report.get("native_suite_totals") or {}
    if totals.get("files", 0) <= 0 or totals.get("tests", 0) <= 0:
        raise ValueError("OpenSSL native test suite totals are missing")
    if set(report["runtime_files_sha256"]) != set(manifest["runtime_files"]):
        raise ValueError("OpenSSL build does not cover every runtime library/provider/CLI")
    paths = []
    for name in sorted(PACKAGES):
        package = report["packages"][name]
        filename = package["file"]
        if Path(filename).name != filename:
            raise ValueError("OpenSSL package must be a direct filename")
        path = Path(directory) / filename
        if path.is_symlink() or not path.is_file() or digest(path) != package["sha256"]:
            raise ValueError("Built OpenSSL package hash differs: " + name)
        paths.append(path)
    if {p.name for p in Path(directory).iterdir()} != {p.name for p in paths}:
        raise ValueError("Unexpected file in OpenSSL package staging")
    return paths


def install(manifest, report, directory, *, run=command):
    paths = check_build(manifest, report, directory)
    if run(["dpkg", "--print-architecture"]) != manifest["architecture"]:
        raise ValueError("OpenSSL package architecture differs")
    audit = run(["dpkg", "--audit"])
    if audit:
        raise ValueError("Existing Debian package state is incomplete:\n" + audit)
    before = inventory(run)
    for name in PACKAGES:
        if before.get(name) != {"status": "installed", "version": manifest["source_version"]}:
            raise ValueError("OpenSSL package differs from the successful baseline: " + name)
    for path in paths:
        name = run(["dpkg-deb", "--field", str(path), "Package"])
        if name not in PACKAGES:
            raise ValueError("Unexpected OpenSSL package name")
        for field, expected in {"Version": manifest["package_version"], "Architecture": manifest["architecture"]}.items():
            if run(["dpkg-deb", "--field", str(path), field]) != expected:
                raise ValueError("OpenSSL package metadata differs: " + field)
        source = run(["dpkg-deb", "--field", str(path), "Source"])
        if source not in {"openssl", "openssl (" + manifest["package_version"] + ")"}:
            raise ValueError("OpenSSL package source differs")
    # Scoped to these three packages, so dpkg verification needs no exclusions.
    run(["dpkg", "--path-include=/usr/share/doc/*", "--path-include=/usr/share/man/*",
         "--install", *map(str, paths)])
    audit = run(["dpkg", "--audit"])
    verification = run(["dpkg", "--verify", *sorted(PACKAGES)])
    if audit or verification:
        raise ValueError("OpenSSL package integrity failed\naudit:\n" + audit + "\nverify:\n" + verification)
    changes = validate_changes(before, inventory(run), manifest)
    for name, expected in report["runtime_files_sha256"].items():
        path = Path("/") / name
        if path.is_symlink() or digest(path) != expected:
            raise ValueError("OpenSSL installed file differs from the build: " + name)
    return {"status": "openssl_backport_installed", "package_changes": changes,
            "runtime_files_sha256": report["runtime_files_sha256"],
            "apt_sources_changed": False, "other_packages_changed": False, "public_release_allowed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("deploy/openssl-update.json"))
    parser.add_argument("--report", type=Path, default=Path("deploy/openssl-build.json"))
    parser.add_argument("--packages", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(install(json.loads(args.manifest.read_bytes()), json.loads(args.report.read_bytes()), args.packages), indent=2))
