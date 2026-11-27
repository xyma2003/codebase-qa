"""Backport two official 3.5 fixes into the exact Debian baseline source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from ops.python_expat_build import fetch


def digest(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def run(arguments, *, cwd=None, env=None, timeout=60, log=None):
    if log is None:
        result = subprocess.run(arguments, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise ValueError(repr(arguments) + "\n" + result.stdout + result.stderr)
        return result.stdout.strip()
    try:
        with Path(log).open("w") as target:
            result = subprocess.run(arguments, cwd=cwd, env=env, text=True,
                                    stdout=target, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        print_failure_log(log)
        raise
    if result.returncode:
        print_failure_log(log)
        raise ValueError("OpenSSL build command failed: " + repr(arguments))
    return Path(log).read_text()


def print_failure_log(path):
    text = Path(path).read_text()
    print(text, file=sys.stderr)
    # BuildKit can clip a verbose native log. Repeat the test evidence and the
    # original final diagnostic at the end so that a clipped log stays useful.
    print("OpenSSL native evidence summary (full log SHA256 " + digest(path) + ")", file=sys.stderr)
    for line in text.splitlines():
        if any(marker in line for marker in ("Result:", "Files=", "new_connid_retrans", "not ok", "FAILED")):
            print(line, file=sys.stderr)
    print("\n".join(text.splitlines()[-60:]), file=sys.stderr)


def check_patches(manifest, directory):
    patches = []
    for entry in manifest["patches"]:
        name = entry["file"]
        if Path(name).name != name:
            raise ValueError("OpenSSL patch path must be a filename")
        path = Path(directory) / name
        content = path.read_bytes()
        if digest(path) != entry["sha256"] or not content.startswith(("From " + entry["commit"] + " ").encode()):
            raise ValueError("Official OpenSSL patch hash or commit differs: " + name)
        patches.append(path)
    return patches


def check_quic_log(text):
    lines = [line for line in text.splitlines() if "new_connid_retrans" in line]
    if not lines or any("skip" in line.lower() for line in lines) or "Result: PASS" not in text:
        raise ValueError("Required QUIC retransmission regression was not verified:\n" + text)
    return lines


def gc_control(source, original, harness, output):
    build = source / "build_shared"
    logs = {}
    for name, implementation, expected in (("baseline", original, 1),
                                            ("patched", source / "ssl/quic/quic_stream_map.c", 0)):
        executable = output / ("gc-" + name)
        run(["cc", "-O2", "-DOPENSSL_BUILDING_OPENSSL", '-DSTREAM_MAP_SOURCE="' + str(implementation) + '"',
             "-I" + str(build / "include"), "-I" + str(source / "include"),
             "-I" + str(build), "-I" + str(source), str(harness),
             str(build / "libssl.a"), str(build / "libcrypto.a"),
             "-ldl", "-pthread", "-lz", "-lzstd", "-o", str(executable)], timeout=120)
        result = subprocess.run([str(executable)], text=True, capture_output=True, timeout=10)
        (output / ("gc-" + name + ".log")).write_text(result.stdout + result.stderr)
        if result.returncode != expected:
            raise ValueError("GC regression control failed: " + name + "\n" + result.stdout + result.stderr)
        expected_reset = "reset_received: expected=1 actual=" + ("0" if name == "baseline" else "1")
        if expected_reset not in result.stdout:
            raise ValueError("GC control did not demonstrate the reset-stream condition: " + result.stdout)
        logs[name] = {"exit_code": result.returncode, "log_sha256": digest(output / ("gc-" + name + ".log"))}
        executable.unlink()
    return logs


def build(manifest, patch_dir, harness, output, work):
    if (manifest.get("schema"), manifest["source_version"], manifest["architecture"]) != (1, "3.5.9-1~deb13u1", "amd64"):
        raise ValueError("Unexpected OpenSSL baseline")
    if run(["dpkg", "--print-architecture"]) != "amd64":
        raise ValueError("OpenSSL baseline is Linux amd64 only")
    if manifest["build_options"] != "parallel=2" or manifest["build_profiles"] != "noudeb":
        raise ValueError("OpenSSL build/test configuration differs")
    patches = check_patches(manifest, patch_dir)
    output, work = Path(output).resolve(), Path(work).resolve()
    output.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    for entry in manifest["source_files"]:
        name = entry["name"]
        if Path(name).name != name or manifest["source_base_url"] != "https://deb.debian.org/debian/pool/main/o/openssl/":
            raise ValueError("OpenSSL source must come from the pinned Debian pool")
        size = fetch(manifest["source_base_url"] + name, entry["sha256"], work / name, entry["bytes"])
        if size != entry["bytes"]:
            raise ValueError("OpenSSL source size differs: " + name)
    dsc = work / ("openssl_" + manifest["source_version"] + ".dsc")
    source = work / "source"
    run(["dpkg-source", "--require-valid-signature", "--require-strong-checksums", "-x", str(dsc), str(source)],
        timeout=120, log=output / "source-verification.log")
    original = work / "original-stream-map.c"
    shutil.copyfile(source / "ssl/quic/quic_stream_map.c", original)
    series = source / "debian/patches/series"
    with series.open("a") as file:
        file.write("\n")
        for patch in patches:
            name = "codebase-qa-" + patch.name
            shutil.copyfile(patch, source / "debian/patches" / name)
            file.write(name + "\n")
    # Keep the exact built tree for post-build probes. Unapplying/reapplying
    # Debian's Configure patches changes mtimes and forces OpenSSL to reconfigure.
    with (source / "debian/source/local-options").open("x") as file:
        file.write("no-unapply-patches\n")
    run(["dpkg-source", "--before-build", str(source)], log=output / "patch-application.log")
    changed_files = {"ssl/quic/quic_stream_map.c", "ssl/quic/quic_channel.c",
                     "ssl/quic/quic_channel_local.h", "include/internal/quic_channel.h", "test/radix/quic_tests.c",
                     "Configure", "Configurations/10-main.conf", "Configurations/20-debian.conf"}
    patched_hashes = {name: digest(source / name) for name in sorted(changed_files)}
    changelog = source / "debian/changelog"
    header = ("openssl (" + manifest["package_version"] + ") trixie; urgency=high\n\n"
              "  * Local backport of upstream CVE-2026-124709 and CVE-2026-107494 fixes.\n\n"
              " -- Codebase QA Builder <114135364+xyma2003@users.noreply.github.com>  "
              + manifest["changelog_date"] + "\n\n")
    changelog.write_text(header + changelog.read_text())
    environment = dict(os.environ, DEB_BUILD_OPTIONS=manifest["build_options"], DEB_BUILD_PROFILES=manifest["build_profiles"])
    build_log = run(["dpkg-buildpackage", "-B", "-us", "-uc"], cwd=source, env=environment,
                    timeout=1200, log=output / "native-build.log")
    if "Result: PASS" not in build_log:
        raise ValueError("Debian OpenSSL native suite was not verified")
    # Verify the retained source without touching generated build dependencies.
    if {name: digest(source / name) for name in patched_hashes} != patched_hashes:
        raise ValueError("OpenSSL regression source differs from the packaged build")
    quic_log = run(["make", "-C", str(source / "build_shared"), "test", "TESTS=test_quic_radix", "HARNESS_VERBOSE=yes"],
                   env=environment, timeout=180, log=output / "quic-regression.log")
    quic_lines = check_quic_log(quic_log)
    controls = gc_control(source, original, Path(harness).resolve(), output)
    packages = output / "packages"
    packages.mkdir()
    payload = work / "payload"
    built = {}
    for path in sorted(work.glob("*.deb")):
        name = run(["dpkg-deb", "--field", str(path), "Package"])
        if name not in manifest["runtime_packages"]:
            continue
        version = run(["dpkg-deb", "--field", str(path), "Version"])
        architecture = run(["dpkg-deb", "--field", str(path), "Architecture"])
        if name in built or version != manifest["package_version"] or architecture != manifest["architecture"]:
            raise ValueError("Unexpected locally built OpenSSL package: " + name)
        shutil.copyfile(path, packages / path.name)
        run(["dpkg-deb", "--extract", str(path), str(payload)])
        built[name] = {"file": path.name, "sha256": digest(path), "version": version, "architecture": architecture}
    if set(built) != set(manifest["runtime_packages"]):
        raise ValueError("OpenSSL build did not produce all three runtime packages")
    hashes = {}
    for name in manifest["runtime_files"]:
        path = payload / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected OpenSSL runtime file missing: " + name)
        hashes[name] = digest(path)
    totals = re.findall(r"Files=(\d+), Tests=(\d+)", build_log)
    report = {"status": "openssl_official_fixes_backported", "source_version": manifest["source_version"],
              "package_version": manifest["package_version"], "source_dsc_sha256": digest(dsc),
              "debian_signature_required": True, "patches": manifest["patches"],
              "patched_source_files_sha256": patched_hashes,
              "targeted_advisories": manifest["targeted_advisories"], "packages": built,
              "runtime_files_sha256": hashes, "quic_regression_lines": quic_lines, "gc_control": controls,
              "native_suite_totals": {"files": int(totals[-1][0]), "tests": int(totals[-1][1])} if totals else None,
              "native_build_log_sha256": digest(output / "native-build.log"),
              "public_release_allowed": False, "real_model_requests": 0}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("deploy/openssl-update.json"))
    parser.add_argument("--patches", type=Path, default=Path("deploy/openssl-patches"))
    parser.add_argument("--harness", type=Path, default=Path("deploy/openssl-gc-regression.c"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    build(json.loads(args.manifest.read_bytes()), args.patches, args.harness, args.output, args.work)
