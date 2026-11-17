"""Linux CI only: exercise image, TLS proxy and stopped-service refusal."""
import json
import http.client
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import ssl

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs).stdout.strip()


def main(image, proxy_image, output):
    suffix = str(os.getpid())
    app, proxy, network = "qa-app-" + suffix, "qa-proxy-" + suffix, "qa-network-" + suffix
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        config = (ROOT / "deploy/nginx.conf.template").read_text().replace("__HOSTNAME__", "localhost")
        (temp / "nginx.conf").write_text(config)
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost", "-keyout", str(temp / "privkey.pem"), "-out", str(temp / "fullchain.pem"))
        # Only synthetic CI TLS material; real deployment keys stay private.
        temp.chmod(0o755)
        (temp / "privkey.pem").chmod(0o644)
        try:
            run("docker", "network", "create", network)
            denied = subprocess.run(["docker", "run", "--rm", image, "--check"], capture_output=True, text=True, timeout=30)
            if denied.returncode == 0:
                raise AssertionError("Missing deployment prerequisites must refuse startup")
            # This direct smoke launch has no OIDC/model config. Business pages
            # still deny access. It is not a production entrypoint bypass flag.
            run("docker", "run", "-d", "--name", app, "--network", network, "--network-alias", "app", "--read-only",
                "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--entrypoint", "python", image,
                "-m", "streamlit", "run", "app.py", "--server.address", "0.0.0.0", "--server.port", "8501", "--server.headless", "true")
            run("docker", "run", "--rm", "--user", "101:101", "--entrypoint", "nginx", "-v", str(temp / "nginx.conf") + ":/etc/nginx/nginx.conf:ro",
                "-v", str(temp) + ":/run/tls:ro", "--network", network, proxy_image, "-t")
            run("docker", "run", "-d", "--name", proxy, "--user", "101:101", "--network", network, "--read-only", "--tmpfs", "/tmp",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-p", "127.0.0.1::8443", "--entrypoint", "nginx",
                "-v", str(temp / "nginx.conf") + ":/etc/nginx/nginx.conf:ro", "-v", str(temp) + ":/run/tls:ro", proxy_image, "-g", "daemon off;")
            port = run("docker", "port", proxy, "8443/tcp").rsplit(":", 1)[1]
            context = ssl.create_default_context(cafile=str(temp / "fullchain.pem"))
            url = "https://localhost:" + port
            deadline = time.monotonic() + 45
            while True:
                try:
                    with urllib.request.urlopen(url + "/healthz", context=context, timeout=3) as response:
                        assert response.status == 200 and response.read().strip() == b"ok"
                        assert response.headers["X-Content-Type-Options"] == "nosniff"
                    break
                except (OSError, AssertionError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            with urllib.request.urlopen(url, context=context, timeout=5) as response:
                assert response.status == 200
            connection = http.client.HTTPSConnection("localhost", int(port), context=context, timeout=5)
            try:
                connection.request("GET", "/_stcore/stream", headers={"Upgrade": "websocket", "Connection": "Upgrade",
                    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Protocol": "streamlit", "Origin": url})
                assert connection.getresponse().status == 101
            finally:
                connection.close()
            limited = False
            for _ in range(35):
                try:
                    with urllib.request.urlopen(url + "/healthz", context=context, timeout=3):
                        pass
                except urllib.error.HTTPError as error:
                    if error.code != 429:
                        raise
                    limited = True
            assert limited, "Proxy rate limit did not reject a burst"
            assert run("docker", "exec", app, "id", "-u") == "10001"
            run("docker", "exec", app, "python", "scripts/healthcheck.py")
            report = {"status": "linux_container_preparation_passed", "image": image, "proxy": proxy_image,
                      "tls_verified": True, "websocket_upgraded": True, "unconfigured_startup_refused": True,
                      "proxy_rate_limit_checked": limited,
                      "application_uid": 10001, "real_model_requests": 0, "real_oidc_callback_tested": False,
                      "public_release_allowed": False}
            (output / "container.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
        finally:
            for name in (app, proxy):
                result = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
                (output / (name + ".log")).write_text(result.stdout + result.stderr)
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
