"""Anonymous public GitHub transport, without checkout, hooks or protocol fallback."""

import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import httpx

from security import resources as limits
from security.output import safe_error


def require_public_repository(repo_url):
    from indexer.snapshots import normalize_repo_url
    url = normalize_repo_url(repo_url)
    owner_repo = url.removeprefix("https://github.com/")
    # Explicitly omit credentials, ambient proxies and redirects.
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
        with client.stream("GET", "https://api.github.com/repos/"+owner_repo,
                           headers={"User-Agent": "codebase-qa", "Accept": "application/vnd.github+json"}) as response:
            if response.status_code != 200:
                raise ValueError("无法匿名确认公开仓库，请检查仓库地址或 GitHub 访问限制。")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 65536:
                    raise ValueError("仓库信息响应超过安全上限。")
            import json
            data = json.loads(body)
    if data.get("private") is not False or str(data.get("full_name", "")).casefold() != owner_repo:
        raise ValueError("仅支持已匿名确认的公开 GitHub 仓库。")


def _disk_bytes(path):
    total = 0
    pending = [Path(path)]
    while pending:
        directory = pending.pop()
        if not directory.exists():
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                else:
                    total += entry.stat(follow_symlinks=False).st_size
                if total > limits.MAX_GIT_BYTES:
                    return total
    return total


def _stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def run_git(arguments, *, cwd, size_path):
    command = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
               "-c", "http.followRedirects=false", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
               "-c", "http.extraHeader=", "-c", "http.extraHeader=Authorization:", *arguments]
    environment = {name: os.environ[name] for name in ("PATH", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR") if name in os.environ}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/usr/bin/false")
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=output, start_new_session=True)
        started = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic()-started > limits.MAX_GIT_SECONDS:
                    raise ValueError("Git 操作超过安全时间上限。")
                if _disk_bytes(size_path) > limits.MAX_GIT_BYTES or os.fstat(output.fileno()).st_size > limits.MAX_LOG_BYTES:
                    raise ValueError("Git 操作超过磁盘或日志安全上限。")
                time.sleep(0.05)
            if _disk_bytes(size_path) > limits.MAX_GIT_BYTES or os.fstat(output.fileno()).st_size > limits.MAX_LOG_BYTES:
                raise ValueError("Git 操作超过磁盘或日志安全上限。")
            if process.returncode:
                output.seek(0)
                message = output.read(limits.MAX_LOG_BYTES).decode(errors="replace")
                raise ValueError("Git 操作失败："+safe_error(RuntimeError(message)))
        finally:
            _stop(process)
