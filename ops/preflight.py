"""Deployment readiness is stricter than HTTP liveness and never calls a model."""
import json
import hashlib
import os
from pathlib import Path
import stat
import tomllib
from urllib.parse import urlsplit

from dotenv import dotenv_values
from ops.dependency_audit import release_gate
from ops.os_audit import validate_os_audit
from security.identity import load_access


def verify_embeddings(cache, manifest):
    cache = Path(cache).resolve()
    expected = json.loads(Path(manifest).read_text())
    if not expected.get("files") or expected.get("model") != "sentence-transformers/all-MiniLM-L6-v2":
        raise ValueError("嵌入缓存清单无效。")
    for name, checksum in expected["files"].items():
        path = (cache / name).resolve()
        if not path.is_relative_to(cache) or not path.is_file():
            raise ValueError("嵌入缓存缺失或路径越界。")
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while block := file.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != checksum:
            raise ValueError("嵌入缓存摘要不符。")


def private_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("运行配置文件缺失或无效。")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("运行配置文件必须仅当前用户可读。")
    return path


def preflight(root, audit_path, lock_path, origin):
    root = Path(root)
    if os.environ.get("CODEBASE_QA_LOCAL_MODE") == "1":
        raise ValueError("部署入口禁止本地操作员模式。")
    release_gate(json.loads(Path(audit_path).read_text()), lock_path)
    validate_os_audit(json.loads(Path(audit_path).with_name("os-audit.json").read_text()), lock_path,
        (root / "deploy/revision").read_text().strip(), os.environ.get("CODEBASE_QA_IMAGE_ID", ""))
    url = urlsplit(origin)
    if (url.scheme != "https" or not url.hostname or url.hostname in {"localhost", "127.0.0.1", "::1"}
            or url.hostname.endswith((".invalid", ".example")) or url.username or url.password or url.path not in {"", "/"}
            or url.query or url.fragment):
        raise ValueError("必须配置真实的 HTTPS 公开地址。")
    access = load_access(private_file(root / ".streamlit/access.toml"))
    auth = tomllib.loads(private_file(root / ".streamlit/secrets.toml").read_text())["auth"]
    if (auth.get("redirect_uri") != origin.rstrip("/") + "/oauth2callback"
            or not auth.get("client_id") or len(auth.get("client_secret", "")) < 8
            or len(auth.get("cookie_secret", "")) < 32 or auth.get("expose_tokens", [])
            or not auth.get("server_metadata_url", "").startswith(access["issuer"].rstrip("/") + "/")):
        raise ValueError("OIDC 运行配置不完整或回调不符。")
    values = dotenv_values(private_file(root / ".env"))
    if not (values.get("OPENAI_API_KEY") or values.get("ANTHROPIC_API_KEY")):
        raise ValueError("模型密钥尚未配置。")
    if values.get("OPENAI_API_KEY"):
        base = urlsplit(values.get("OPENAI_API_BASE", "https://api.siliconflow.cn/v1"))
        if base.scheme != "https" or not base.hostname or base.username or base.password:
            raise ValueError("模型地址必须使用 HTTPS。")
    for part in ("chroma_db", "repos", ".baseline_runtime/tasks", ".baseline_runtime/reviews", ".baseline_runtime/evaluations"):
        directory = root / part
        if not directory.is_dir() or directory.is_symlink() or not os.access(directory, os.W_OK):
            raise ValueError("持久化目录缺失、不可写或包含符号链接。")
    verify_embeddings(os.environ.get("HF_HOME", root / ".cache/huggingface"), root / "deploy/embedding-cache.json")
    return {"ready": True, "model_called": False, "oidc_callback_tested": False}
