"""Plain-table application databases and a separate read-only backup check.

These controls restrict SQL features; they do not patch the SQLite engine or
make arbitrary database files safe to accept from an untrusted user.
"""
from pathlib import Path
import sqlite3


def _open(database, *, timeout, uri=False):
    db = sqlite3.connect(database, timeout=timeout, uri=uri)
    try:
        for option, enabled in ((sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True),
                                (sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False),
                                (sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, False),
                                (sqlite3.SQLITE_DBCONFIG_ENABLE_FTS3_TOKENIZER, False)):
            db.setconfig(option, enabled)
            if db.getconfig(option) != enabled:
                raise ValueError("SQLite 安全配置未生效。")
        db.enable_load_extension(False)
        return db
    except BaseException:
        db.close()
        raise


def _authorize(action, first, second, database, source):
    if action in (sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in {"load_extension", "fts3_tokenizer", "fts5", "match"}:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        if (first or "").lower() in {"writable_schema", "temp_store_directory", "data_store_directory"}:
            return sqlite3.SQLITE_DENY
        if (first or "").lower() == "trusted_schema" and second is not None and second.lower() not in {"0", "off", "false"}:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def connect(path, *, timeout=5, read_only=False):
    database = Path(path).absolute().as_uri() + "?mode=ro" if read_only else path
    db = _open(database, timeout=timeout, uri=read_only)
    try:
        db.set_authorizer(_authorize)
        # Use SQLite's classification, not string matching against CREATE SQL.
        if any(row[2] in {"virtual", "shadow"} for row in db.execute("PRAGMA table_list")):
            raise ValueError("业务数据库不能包含虚表或虚表内部表。")
        return db
    except BaseException:
        db.close()
        raise


def _integrity_authorizer(action, first, second, database, source):
    if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() == "count":
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and first in {"integrity_check", "data_version"} and second is None:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def check_integrity(path):
    """Also supports the retained legacy Chroma FTS schema, without exposing SQL.

    Only this fixed PRAGMA is executed. The file is opened mode=ro, all writes,
    MATCH, extension loading and attachment are denied, and no connection is
    returned to a caller. Keep the old data and the full integrity check.
    """
    db = _open(Path(path).absolute().as_uri() + "?mode=ro", timeout=5, uri=True)
    try:
        db.set_authorizer(_integrity_authorizer)
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite 完整性检查失败。")
    finally:
        db.close()
