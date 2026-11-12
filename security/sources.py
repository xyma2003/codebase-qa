"""Deny sensitive source material before tools or embeddings receive it."""

from pathlib import PurePosixPath
import re
from security.resources import MAX_SOURCE_BYTES, MAX_SNAPSHOT_BYTES, MAX_INDEX_CHUNKS

# Match the existing whole-source scanning limit; output remains capped at 8000 chars.


class SourcePolicyError(ValueError):
    pass


def sensitive_path(path):
    parts = PurePosixPath(str(path).replace("\\", "/")).parts
    names = [part.casefold() for part in parts]
    name = names[-1] if names else ""
    if any(ord(char) < 32 or char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for char in str(path)):
        return True
    template = name.endswith((".example", ".sample", ".template", ".dist"))
    return (bool(secret_spans(str(path)))
            or any(part in {".ssh", ".aws", ".azure", ".kube", ".gnupg", ".docker"} for part in names)
            or (name.startswith(".env") and not template)
            or name in {".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "secrets"}
            or (not template and (name.startswith(("credentials.", "secrets.")) or name.endswith((".pem", ".key", ".p12", ".pfx", ".jks")))))


_TOKENS = re.compile(r"(?<![A-Za-z0-9])(?:sk-(?:(?:proj-|ant-(?:api03-)?)?)[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16})(?![A-Za-z0-9])")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")
_ASSIGNMENT = re.compile(r'''(?im)["']?\b(?:[A-Z][A-Z0-9_]*_)?(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|SECRET_KEY|CLIENT_SECRET|PASSWORD)["']?\s*[:=]\s*(["']?)([^\s"'`,;{}\\]+)''')


def _placeholder(value, *, reference=False):
    lowered = value.lower()
    return (not value or lowered.startswith(("your-", "your_", "<", "${", "os.getenv(", "os.environ", "process.env.", "getenv("))
            or re.fullmatch(r"sk-(?:ant-)?x{20,}", value) is not None
            or lowered in {"none", "null", "false", "true", "changeme", "placeholder"}
            or value in {"FIXTURE_SECRET_NOT_REAL", "FAKE_AUDIT_VALUE_NOT_A_SECRET", "LOCAL_TEST_NOT_A_SECRET"}
            or (reference and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None))


def configured_secrets():
    # Values are compared locally and are never included in exceptions or policy results.
    from config import OPENAI_API_KEY, ANTHROPIC_API_KEY
    return tuple(value for value in (OPENAI_API_KEY, ANTHROPIC_API_KEY) if len(value) >= 8)


def secret_spans(text, *, allow_references=True):
    spans = [(match.start(), match.end()) for match in _PRIVATE_KEY.finditer(text)]
    spans += [(match.start(), match.end()) for match in _TOKENS.finditer(text) if not _placeholder(match.group())]
    spans += [(match.start(2), match.end(2)) for match in _ASSIGNMENT.finditer(text)
              if len(match[2]) >= 8 and not _placeholder(match[2], reference=allow_references and not bool(match[1]))]
    for secret in configured_secrets():
        start = 0
        while (start := text.find(secret, start)) >= 0:
            spans.append((start, start + len(secret)))
            start += len(secret)
    return spans


def ensure_safe_text(text, *, allow_references=True):
    if secret_spans(text, allow_references=allow_references):
        raise SourcePolicyError("内容包含疑似凭据或私钥，已阻止传递。")


def ensure_safe_source(path, data):
    if sensitive_path(path):
        raise SourcePolicyError("安全策略禁止读取敏感文件。")
    if len(data) > MAX_SOURCE_BYTES:
        raise SourcePolicyError("文件超过源码读取字节上限。")
    name = PurePosixPath(path).name.casefold()
    references = not (name.startswith(".env") or name.endswith((".yaml", ".yml", ".json", ".toml")))
    ensure_safe_text(data.decode("utf-8", errors="replace"), allow_references=references)


def safe_chunks(chunks, snapshot):
    """Recheck whole files, including files in snapshots created before this policy."""
    from indexer.source_reader import read_source_bytes, SourceReadError
    allowed, result = {}, []
    for chunk in chunks:
        if chunk.file_path not in allowed:
            try:
                if chunk.file_path not in snapshot.files:
                    raise SourceReadError("Excluded source")
                read_source_bytes(snapshot.root, chunk.file_path)
            except (SourcePolicyError, SourceReadError):
                allowed[chunk.file_path] = False
            else:
                allowed[chunk.file_path] = True
        if allowed[chunk.file_path]:
            ensure_safe_text(chunk.content)
            result.append(chunk)
    return result


def check_chunks(chunks):
    if len(chunks) > MAX_INDEX_CHUNKS or sum(len(chunk.content.encode()) for chunk in chunks) > MAX_SNAPSHOT_BYTES:
        raise SourcePolicyError("索引内容超过安全上限。")
    for chunk in chunks:
        ensure_safe_source(chunk.file_path, chunk.content.encode())


def secure_chunk_repo(snapshot):
    """Check size and content before parsing; preserve the original parsing functions."""
    import hashlib
    from pathlib import PurePosixPath
    from config import CODE_EXTENSIONS
    from indexer.chunker import _parse_python, _sliding_window
    from indexer.code_search import SKIP_DIRECTORIES
    from indexer.source_reader import read_source_bytes, SourceReadError
    total, count = 0, 0
    for path, meta in snapshot.files.items():
        suffix = PurePosixPath(path).suffix.lower()
        if (suffix not in CODE_EXTENSIONS or SKIP_DIRECTORIES.intersection(PurePosixPath(path).parts[:-1])
                or not meta.get("materialized", True) or meta["bytes"] > MAX_SOURCE_BYTES):
            continue
        try:
            _, data = read_source_bytes(snapshot.root, path)
        except SourceReadError:
            # A blocked legacy file never reaches the parser or embedding model.
            continue
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise SourcePolicyError("索引前源码校验失败。")
        if b"\x00" in data:
            continue
        text = data.decode("utf-8", errors="ignore")
        if not text.strip():
            continue
        for chunk in (_parse_python(text, path) if suffix == ".py" else _sliding_window(text, path)):
            count += 1
            total += len(chunk.content.encode())
            if count > MAX_INDEX_CHUNKS or total > MAX_SNAPSHOT_BYTES:
                raise SourcePolicyError("索引内容超过安全上限。")
            yield chunk
