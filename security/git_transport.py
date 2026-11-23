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
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as error:
            # A short-lived child can exit between poll() and killpg(). Some
            # macOS execution environments report EPERM for that race. Accept
            # only a confirmed exit; never swallow failure to stop a live child.
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise error
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def _validate_operation(arguments, *, cwd, size_path):
    """Accept only the two transport operations used by repository import.

    In particular, clone must never check out attacker-controlled trees. This
    restriction also covers nonrecursive checkout attacks (CVE-2026-111760),
    rather than relying on the absence of --recurse-submodules alone.
    """
    from indexer.snapshots import normalize_repo_url
    if not isinstance(arguments, (list, tuple)) or not all(isinstance(arg, str) and "\0" not in arg for arg in arguments):
        raise ValueError("Git 操作不在允许范围内。")
    arguments = list(arguments)
    directory = Path(cwd)
    if directory.is_symlink() or not directory.is_dir() or Path(size_path).resolve() != directory.resolve():
        raise ValueError("Git 操作目录与资源检查目录不一致。")
    if len(arguments) == 7 and arguments[:5] == ["clone", "--no-checkout", "--depth=1", "--no-tags", "--"]:
        url, target = arguments[5:]
        path = Path(target)
        if (not path.is_absolute() or path.parent != directory.resolve() or path.is_symlink()
                or path.exists() or target != str(path.resolve())):
            raise ValueError("Git 克隆只能写入临时目录中的新目录。")
    elif len(arguments) == 5 and arguments[:3] == ["fetch", "--depth=1", "--no-tags"] and arguments[-1] == "HEAD":
        url = arguments[3]
    else:
        raise ValueError("Git 仅允许不检出代码的浅克隆和默认分支更新。")
    if url != normalize_repo_url(url):
        raise ValueError("Git 仅允许规范化的 GitHub HTTPS 地址。")
    return arguments


def run_git(arguments, *, cwd, size_path):
    arguments = _validate_operation(arguments, cwd=cwd, size_path=size_path)
    command = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
               "-c", "http.followRedirects=false", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
               "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false",
               "-c", "http.proxy=", "-c", "http.emptyAuth=false", "-c", "http.delegation=none",
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
