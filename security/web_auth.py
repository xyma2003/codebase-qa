"""Every Streamlit page enters through this gate before reading private data."""

import os
from urllib.parse import urlsplit
import streamlit as st

from security.identity import AccessDenied, LOCAL_OPERATOR, bind_principal, load_access, verified_principal
from security.sources import register_credentials


def _logout():
    bind_principal(None)
    st.session_state.clear()
    st.logout()


def _clear_private_state():
    # Keep the pending auth-button event so a rerun can initiate login/logout.
    for key in list(st.session_state):
        if key not in {"auth_login","auth_logout"}:
            del st.session_state[key]


def _sync_session(principal):
    marker = principal.owner_id+":"+principal.role
    if st.session_state.get("_verified_account") != marker:
        if st.session_state.get("_verified_account") is not None or principal.owner_id != "local":
            st.session_state.clear()
        st.session_state["_verified_account"] = marker


def _access_settings():
    access = load_access(os.environ.get("CODEBASE_QA_ACCESS_FILE"))
    auth = st.secrets["auth"]
    register_credentials((auth.get("client_secret"),auth.get("cookie_secret")))
    redirect = urlsplit(auth.get("redirect_uri", ""))
    if (not all(auth.get(name) for name in ("redirect_uri", "cookie_secret", "client_id", "client_secret", "server_metadata_url"))
            or not auth["server_metadata_url"].startswith(access["issuer"].rstrip("/")+"/")
            or len(auth["cookie_secret"]) < 32 or auth["cookie_secret"].startswith("your-")
            or (redirect.scheme != "https" and not (redirect.scheme == "http" and redirect.hostname in {"localhost","127.0.0.1","::1"}))
            or auth.get("expose_tokens", [])):
        raise AccessDenied("登录配置尚未完成或无效。")
    return access


def callback_principal():
    # Streamlit callbacks execute before the page body; never reuse a principal
    # left by a previous render when a token expires or an invitation is revoked.
    bind_principal(None)
    if os.environ.get("CODEBASE_QA_LOCAL_MODE") == "1":
        if st.get_option("server.address") not in {"127.0.0.1", "::1", "localhost"}:
            raise AccessDenied("本地模式只允许监听本机地址。")
        principal = LOCAL_OPERATOR
    else:
        try:
            principal = verified_principal(st.user.to_dict(), _access_settings())
        except Exception:
            raise AccessDenied("登录已失效或没有访问权限。") from None
    bind_principal(principal)
    return principal


def require_web_user(*, admin=False):
    bind_principal(None)
    local = os.environ.get("CODEBASE_QA_LOCAL_MODE") == "1"
    if local:
        if st.get_option("server.address") not in {"127.0.0.1", "::1", "localhost"}:
            st.error("本地模式只允许监听本机地址。")
            st.stop()
        principal = LOCAL_OPERATOR
    else:
        try:
            access = _access_settings()
        except Exception:
            _clear_private_state()
            st.error("登录配置尚未完成或无效。")
            st.stop()
        claims = st.user.to_dict()
        if claims.get("is_logged_in") is not True:
            _clear_private_state()
            if st.button("登录", key="auth_login"):
                try:
                    st.login()
                except Exception:
                    st.error("登录失败，请联系管理员。")
            st.stop()
        try:
            principal = verified_principal(claims, access)
        except AccessDenied as exc:
            _clear_private_state()
            st.error(str(exc))
            st.button("退出登录", on_click=_logout, key="auth_logout")
            st.stop()
    _sync_session(principal)
    bind_principal(principal)
    if not local:
        st.sidebar.button("退出登录", on_click=_logout, key="auth_logout")
    if admin and principal.role != "admin":
        st.error("没有管理权限。")
        st.stop()
    return principal
