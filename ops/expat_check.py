"""Verify the installed system library and identify Python's separate Expat."""
import ctypes
import ctypes.util
import hashlib
import json
from pathlib import Path
import pyexpat
import re
import subprocess
import sys


def parse_version(value):
    match = re.fullmatch(r"(?:expat_)?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError("Unknown Expat runtime version: " + value)
    return tuple(map(int, match.groups()))


def check_native_parser(library):
    library.XML_ParserCreate.argtypes = [ctypes.c_char_p]
    library.XML_ParserCreate.restype = ctypes.c_void_p
    library.XML_Parse.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    library.XML_Parse.restype = ctypes.c_int
    library.XML_ParserReset.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    library.XML_ParserReset.restype = ctypes.c_ubyte
    library.XML_ParserFree.argtypes = [ctypes.c_void_p]
    library.XML_ParserFree.restype = None
    parser = library.XML_ParserCreate(None)
    if not parser:
        raise ValueError("Native Expat parser allocation failed")
    try:
        for document in (b"<root><item>first</item></root>", b"<root>second</root>"):
            if library.XML_Parse(parser, document, len(document), 1) != 1:
                raise ValueError("Native Expat parse failed")
            if library.XML_ParserReset(parser, None) != 1:
                raise ValueError("Native Expat reset failed")
    finally:
        library.XML_ParserFree(parser)


def verify(manifest_path=Path("deploy/expat-update.json")):
    manifest = json.loads(Path(manifest_path).read_bytes())
    installed = subprocess.check_output(["dpkg-query", "--show", "--showformat=${Version}", "libexpat1"], text=True).strip()
    if installed != manifest["after"]:
        raise ValueError("Installed Expat does not match the pinned package")
    name = ctypes.util.find_library("expat")
    if not name:
        raise ValueError("System Expat library not found")
    library = ctypes.CDLL(name)
    library.XML_ExpatVersion.argtypes = []
    library.XML_ExpatVersion.restype = ctypes.c_char_p
    native_version = library.XML_ExpatVersion().decode("ascii")
    if parse_version(native_version) != (2, 11, 0):
        raise ValueError("System Expat loader did not select the updated library")
    check_native_parser(library)
    # The official Python image may bundle another copy. A dpkg update cannot
    # establish that this copy was patched, so archive its actual version too.
    python_version = pyexpat.EXPAT_VERSION
    python_parser = pyexpat.ParserCreate()
    python_parser.Parse(b"<root>python</root>", True)
    linkage = subprocess.run(["ldd", pyexpat.__file__], capture_output=True, text=True, timeout=10)
    if linkage.returncode or "not found" in linkage.stdout + linkage.stderr:
        raise ValueError("Python Expat native dependencies are incomplete")
    extension_sha256 = hashlib.sha256(Path(pyexpat.__file__).read_bytes()).hexdigest()
    build_path = Path(manifest_path).parent / "python-expat-build.json"
    if build_path.exists():
        build = json.loads(build_path.read_bytes())
        source = json.loads((build_path.parent / "python-expat.json").read_bytes())
        if (sys.version_info[:3] != (3, 12, 13) or python_version != source["expat_version"]
                or "libexpat.so.1" not in linkage.stdout
                or extension_sha256 != build["extension_sha256"]
                or build["source_sha256"] != source["source_sha256"]
                or build["headers_sha256"] != source["headers_sha256"]
                or build["upstream_test_count"] <= 0):
            raise ValueError("Python Expat runtime differs from the verified build")
    return {"status": "system_expat_update_verified", "package_version": installed,
            "system_expat_version": native_version, "system_parse_and_reset_passed": True,
            "python_expat_version": python_version, "python_extension": pyexpat.__file__,
            "python_extension_sha256": extension_sha256,
            "python_linkage": linkage.stdout, "python_parse_passed": True,
            "python_expat_requires_followup": parse_version(python_version) < (2, 11, 0),
            "python_build_bound_to_runtime": build_path.exists(),
            "targeted_advisories": manifest["targeted_advisories"],
            "all_expat_advisories_cleared": False, "public_release_allowed": False,
            "real_model_requests": 0}


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
