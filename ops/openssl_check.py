"""Bind loaded TLS libraries to the backport and test local TLS verification."""
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading

from ops.openssl_build import digest


def tls_check():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, text=True, timeout=30)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(3)
        listener.settimeout(5)
        address = listener.getsockname()
        errors, successful = [], []
        def serve():
            try:
                for _ in range(3):
                    connection, _ = listener.accept()
                    connection.settimeout(5)
                    try:
                        with server_context.wrap_socket(connection, server_side=True) as secured:
                            if secured.recv(4) != b"ping":
                                raise ValueError("Unexpected TLS fixture request")
                            secured.sendall(b"pong")
                            successful.append(True)
                    except ssl.SSLError:
                        connection.close()  # Expected for the two rejected clients.
            except BaseException as error:
                errors.append(repr(error))
            finally:
                listener.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        trusted = ssl.create_default_context(cafile=str(cert))
        with socket.create_connection(address, timeout=5) as raw:
            with trusted.wrap_socket(raw, server_hostname="localhost") as secured:
                protocol = secured.version()
                secured.sendall(b"ping")
                if secured.recv(4) != b"pong" or protocol not in {"TLSv1.2", "TLSv1.3"}:
                    raise ValueError("TLS fixture round trip failed")
        rejected = []
        for context, hostname in ((trusted, "wrong.invalid"), (ssl.create_default_context(), "localhost")):
            try:
                with socket.create_connection(address, timeout=5) as raw:
                    with context.wrap_socket(raw, server_hostname=hostname):
                        pass
            except ssl.SSLCertVerificationError:
                rejected.append(hostname)
            else:
                raise ValueError("TLS accepted an invalid certificate or hostname")
        thread.join(timeout=5)
        if thread.is_alive() or errors or successful != [True] or len(rejected) != 2:
            raise ValueError("TLS server verification failed: " + repr(errors))
        return {"protocol": protocol, "verified_round_trip": True,
                "wrong_hostname_rejected": True, "untrusted_certificate_rejected": True}


def verify(root=Path("/app")):
    report = json.loads((root / "deploy/openssl-build.json").read_bytes())
    manifest = json.loads((root / "deploy/openssl-update.json").read_bytes())
    if report["patches"] != manifest["patches"] or report["package_version"] != manifest["package_version"]:
        raise ValueError("OpenSSL runtime manifest differs from the verified build")
    hashes = {}
    for name, expected in report["runtime_files_sha256"].items():
        path = Path("/") / name
        if path.is_symlink() or digest(path) != expected:
            raise ValueError("OpenSSL runtime file differs: " + name)
        hashes[name] = expected
    mappings = Path("/proc/self/maps").read_text()
    loaded = []
    for name in ("libssl.so.3", "libcrypto.so.3"):
        entries = [line for line in mappings.splitlines() if name in line]
        if not entries or any("(deleted)" in line for line in entries):
            raise ValueError("Python did not load the current OpenSSL library: " + name)
        for line in entries:
            path = Path(line.split()[-1])
            if digest(path) != hashes["usr/lib/x86_64-linux-gnu/" + name]:
                raise ValueError("Python loaded another OpenSSL copy: " + str(path))
            loaded.append(str(path))
    if not ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.9 "):
        raise ValueError("OpenSSL ABI/version changed beyond the declared backport")
    from cryptography.hazmat.backends.openssl import backend
    return {"status": "system_openssl_backport_verified", "openssl_version": ssl.OPENSSL_VERSION,
            "loaded_system_libraries": sorted(set(loaded)), "runtime_files_sha256": hashes,
            "tls": tls_check(), "targeted_advisories": manifest["targeted_advisories"],
            "cryptography_separate_backend_version": backend.openssl_version_text(),
            "cryptography_backend_remediated_by_this_update": False,
            "all_openssl_advisories_cleared": False, "public_release_allowed": False, "real_model_requests": 0}


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
