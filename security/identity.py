"""Server-owned identities. Models and browser inputs cannot select an owner."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import time
import tomllib
from urllib.parse import urlsplit


class AccessDenied(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    owner_id: str
    role: str
    expires_at: float | None = None
    access_file: str | None = None

    def __post_init__(self):
        import re
        if (self.owner_id != "local" and not re.fullmatch(r"user-[0-9a-f]{64}", self.owner_id)) or self.role not in {"user", "admin"}:
            raise AccessDenied("用户身份无效。")


LOCAL_OPERATOR = Principal("local", "admin")
_actor = ContextVar("codebase_qa_principal", default=None)
ROOT = Path(__file__).resolve().parents[1]


def user_id(issuer, subject):
    if (not isinstance(issuer,str) or not issuer or any(ord(c)<32 for c in issuer)
            or not isinstance(subject, str) or not subject or len(subject) > 512):
        raise AccessDenied("登录身份无效。")
    return "user-"+hashlib.sha256((issuer+"\0"+subject).encode()).hexdigest()


def load_access(path=None):
    path = Path(path) if path else ROOT/".streamlit/access.toml"
    try:
        if path.is_symlink():
            raise ValueError
        with path.open("rb") as file:
            raw = file.read(1024*1024+1)
        if len(raw) > 1024*1024:
            raise ValueError
        data = tomllib.loads(raw.decode())
        issuer = data["issuer"]
        parsed = urlsplit(issuer)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not isinstance(data["users"], list)):
            raise ValueError
        users = {}
        for row in data["users"]:
            identity = user_id(issuer, row["subject"])
            if identity in users:
                raise ValueError
            users[identity] = Principal(identity, row["role"])
        if not users:
            raise ValueError
        return {"issuer":issuer, "users":users, "path":str(path.resolve())}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise AccessDenied("登录配置尚未完成或无效。") from None


def verified_principal(claims, access, *, now=None):
    now = time.time() if now is None else now
    if claims.get("is_logged_in") is not True or claims.get("iss") != access["issuer"]:
        raise AccessDenied("请先登录。")
    for name in ("exp", "iat"):
        value = claims.get(name)
        if type(value) not in {int, float} or not math.isfinite(value):
            raise AccessDenied("登录已过期，请重新登录。")
    if claims["exp"] <= now or claims["iat"] > now+60 or claims["iat"] <= 0 or claims["iat"] >= claims["exp"]:
        raise AccessDenied("登录已过期，请重新登录。")
    if "nbf" in claims and (type(claims["nbf"]) not in {int,float} or not math.isfinite(claims["nbf"]) or claims["nbf"] > now+60):
        raise AccessDenied("登录尚未生效，请重新登录。")
    identity = user_id(access["issuer"], claims.get("sub"))
    if identity not in access["users"]:
        raise AccessDenied("此账号没有访问权限。")
    configured = access["users"][identity]
    return Principal(identity, configured.role, claims["exp"], access.get("path"))


def bind_principal(principal):
    return _actor.set(principal)


@contextmanager
def as_principal(principal):
    token = bind_principal(principal)
    try:
        yield principal
    finally:
        _actor.reset(token)


def current_principal():
    principal = _actor.get()
    if principal is not None:
        if principal.expires_at is not None and time.time() >= principal.expires_at:
            raise AccessDenied("登录已过期，请重新登录。")
        if principal.access_file:
            registered = load_access(principal.access_file)["users"].get(principal.owner_id)
            if registered is None or registered.role != principal.role:
                raise AccessDenied("访问权限已变化，请重新登录。")
        return principal
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    if get_script_run_ctx(suppress_warning=True) is not None:
        raise AccessDenied("请先登录。")
    return LOCAL_OPERATOR


def require_admin():
    principal = current_principal()
    if principal.role != "admin":
        raise AccessDenied("没有管理权限。")
    return principal
