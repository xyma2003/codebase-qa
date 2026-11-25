"""Rebuild only pyexpat from the baseline CPython source, using fixed Expat.

The interpreter version, the rest of its standard library, and pip locks stay
unchanged. Compiler, sources and development headers remain in a build stage.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pyexpat
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import urllib.request


def check_baseline(manifest):
    if manifest.get("schema") != 1 or manifest["python_version"] != "3.12.13":
        raise ValueError("Unexpected CPython source manifest")
    if sys.version_info[:3] != (3, 12, 13) or os.environ.get("PYTHON_SHA256") != manifest["source_sha256"]:
        raise ValueError("CPython source does not match the successful baseline image")
    expected = "https://www.python.org/ftp/python/3.12.13/Python-3.12.13.tar.xz"
    if manifest["source_url"] != expected:
        raise ValueError("CPython source must come from the pinned official release")
    headers = "https://deb.debian.org/debian/pool/main/e/expat/libexpat1-dev_2.11.0-1_amd64.deb"
    if manifest["headers_url"] != headers or manifest["expat_version"] != "expat_2.11.0":
        raise ValueError("Expat development headers do not match the fixed system library")


def fetch(url, expected_sha256, destination, maximum_bytes, *, opener=None):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    digest, total = hashlib.sha256(), 0
    destination = Path(destination)
    created = False
    try:
        with opener.open(url, timeout=30) as response:
            if response.geturl() != url:
                raise ValueError("Pinned source download redirected")
            with destination.open("xb") as target:
                created = True
                while chunk := response.read(65536):
                    total += len(chunk)
                    if total > maximum_bytes:
                        raise ValueError("Pinned source download exceeds its size limit")
                    digest.update(chunk)
                    target.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("Pinned source SHA256 mismatch")
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return total


def build(manifest, output, work):
    check_baseline(manifest)
    output, work = Path(output), Path(work)
    output.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    source_archive = work / "python.tar.xz"
    fetch(manifest["source_url"], manifest["source_sha256"], source_archive, manifest["source_max_bytes"])
    with tarfile.open(source_archive) as archive:
        archive.extractall(work / "source", filter="data")
    source = work / "source" / ("Python-" + manifest["python_version"])
    headers_archive = work / "headers.deb"
    size = fetch(manifest["headers_url"], manifest["headers_sha256"], headers_archive, manifest["headers_bytes"])
    if size != manifest["headers_bytes"]:
        raise ValueError("Pinned Expat development package size mismatch")
    headers = work / "headers"
    subprocess.run(["dpkg-deb", "--extract", str(headers_archive), str(headers)], check=True)
    config = work / "config"
    config.mkdir()
    shutil.copyfile(source / "Modules/expat/expat_config.h", config / "expat_config.h")
    module_dir = output / "module"
    module_dir.mkdir()
    extension = module_dir / Path(pyexpat.__file__).name
    include = Path(sysconfig.get_path("include"))
    flags = []
    for name in ("LDSHARED", "CFLAGS", "CCSHARED", "LDFLAGS"):
        flags.extend(shlex.split(sysconfig.get_config_var(name) or ""))
    compiler = [*flags, "-I" + str(config), "-I" + str(headers / "usr/include"),
                "-I" + str(include), "-I" + str(include / "internal"),
                str(source / "Modules/pyexpat.c"), "-o", str(extension), "-l:libexpat.so.1"]
    subprocess.run(compiler, check=True, timeout=120)
    extension.chmod(0o755)
    environment = dict(os.environ, PYTHONPATH=str(module_dir.resolve()) + os.pathsep + str(source / "Lib"))
    probe = subprocess.run([sys.executable, "-c",
        "import json,pyexpat,xml.etree.ElementTree as E; "
        "assert E.fromstring('<root>ok</root>').text=='ok'; "
        "print(json.dumps({'version':pyexpat.EXPAT_VERSION,'module':pyexpat.__file__}))"],
        env=environment, capture_output=True, text=True, timeout=30)
    if probe.returncode:
        raise ValueError("Rebuilt Python XML probe failed:\n" + probe.stdout + probe.stderr)
    loaded = json.loads(probe.stdout)
    if loaded["version"] != manifest["expat_version"] or Path(loaded["module"]).resolve() != extension.resolve():
        raise ValueError("Python did not load the rebuilt Expat module")
    log_path = output / "unittest.log"
    with log_path.open("w") as log:
        tests = subprocess.run([sys.executable, "-m", "unittest", "-v", *manifest["upstream_tests"]],
                               env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=180)
    log_text = log_path.read_text()
    if tests.returncode:
        print(log_text, file=sys.stderr)
        raise ValueError("Official CPython XML regression tests failed")
    summary = re.search(r"Ran (\d+) tests in ([\d.]+)s", log_text)
    if not summary:
        raise ValueError("Official CPython XML test summary is missing")
    if list(module_dir.iterdir()) != [extension]:
        raise ValueError("The runtime overlay must contain only the pyexpat extension")
    report = {"status": "baseline_pyexpat_rebuilt", "python_version": manifest["python_version"],
              "source_sha256": manifest["source_sha256"], "headers_sha256": manifest["headers_sha256"],
              "previous_expat_version": pyexpat.EXPAT_VERSION, "rebuilt_expat_version": loaded["version"],
              "extension": extension.name, "extension_sha256": hashlib.sha256(extension.read_bytes()).hexdigest(),
              "compiler_command": compiler, "upstream_tests": manifest["upstream_tests"],
              "upstream_test_count": int(summary[1]), "upstream_test_seconds": float(summary[2]),
              "upstream_test_log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
              "python_version_unchanged": True, "dependency_locks_unchanged": True,
              "public_release_allowed": False}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    shutil.rmtree(work)
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("deploy/python-expat.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    build(json.loads(args.manifest.read_bytes()), args.output, args.work)
