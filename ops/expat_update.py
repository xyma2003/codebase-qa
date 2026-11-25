"""Install one hash-pinned Debian library, without enabling another APT suite."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("Debian package command failed: " + repr(arguments) + "\n" + result.stdout + result.stderr)
    return result.stdout.strip()


def inventory(run=command):
    text = run(["dpkg-query", "--show", "--showformat=${binary:Package}\t${db:Status-Status}\t${Version}\n"])
    packages = {}
    for line in text.splitlines():
        name, status, version = line.split("\t")
        name = name.split(":")[0]
        if name in packages:
            raise ValueError("Unexpected duplicate package architecture: " + name)
        packages[name] = {"status": status, "version": version}
    return packages


def validate_changes(before, after, manifest):
    package = manifest["package"]
    expected = {package: {"before": {"status": "installed", "version": manifest["before"]},
                          "after": {"status": "installed", "version": manifest["after"]}}}
    changes = {name: {"before": before.get(name), "after": after.get(name)}
               for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    if changes != expected:
        raise ValueError("Only the declared Expat update is allowed: " + json.dumps(changes, sort_keys=True))
    return changes


def download(manifest, destination, *, opener=None):
    expected_url = "https://deb.debian.org/debian/pool/main/e/expat/libexpat1_" + manifest["after"] + "_amd64.deb"
    if manifest["url"] != expected_url or not 0 < manifest["bytes"] <= 2 * 1024 * 1024:
        raise ValueError("Unexpected Debian package URL or size")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(manifest["url"], timeout=30) as response:
        if response.geturl() != manifest["url"]:
            raise ValueError("Debian package download redirected")
        data = response.read(manifest["bytes"] + 1)
    if len(data) != manifest["bytes"] or hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("Debian package size or SHA256 mismatch")
    Path(destination).write_bytes(data)


def install(manifest, *, run=command, fetch=download):
    if (manifest.get("schema"), manifest.get("package"), manifest.get("source"), manifest.get("architecture")) != (1, "libexpat1", "expat", "amd64"):
        raise ValueError("Unexpected Expat update manifest")
    if run(["dpkg", "--print-architecture"]) != manifest["architecture"]:
        raise ValueError("Pinned Debian package does not match this architecture")
    if run(["dpkg", "--audit"]):
        raise ValueError("Existing Debian package state is incomplete")
    before = inventory(run)
    if before.get(manifest["package"]) != {"status": "installed", "version": manifest["before"]}:
        raise ValueError("Installed Expat differs from the verified baseline")
    with tempfile.TemporaryDirectory(prefix="expat-update-") as directory:
        path = str(Path(directory) / "libexpat1.deb")
        fetch(manifest, path)
        for field, expected in {"Package": manifest["package"], "Source": manifest["source"],
                                "Version": manifest["after"], "Architecture": manifest["architecture"],
                                "Pre-Depends": manifest["pre_depends"]}.items():
            if run(["dpkg-deb", "--field", path, field]) != expected:
                raise ValueError("Pinned Debian package metadata differs: " + field)
        # dpkg checks existing dependencies; it cannot fetch/upgrade anything else.
        run(["dpkg", "--install", path])
    audit_output = run(["dpkg", "--audit"])
    verification_output = run(["dpkg", "--verify", manifest["package"]])
    if audit_output or verification_output:
        raise ValueError("Installed Expat package verification failed\n"
                         + "dpkg --audit:\n" + audit_output + "\n"
                         + "dpkg --verify:\n" + verification_output)
    after = inventory(run)
    changes = validate_changes(before, after, manifest)
    return {"status": "pinned_expat_package_installed", "package_changes": changes,
            "package_sha256": manifest["sha256"], "package_source": manifest["url"],
            "apt_sources_changed": False, "other_packages_changed": False,
            "public_release_allowed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("deploy/expat-update.json"))
    args = parser.parse_args()
    print(json.dumps(install(json.loads(args.manifest.read_bytes())), indent=2))
