"""Actual SQLite engine probes, also run inside the deployment image."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from security.sqlite import connect, check_integrity


def verify():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = connect(root / "plain.sqlite3")
        try:
            db.execute("CREATE TABLE data (value TEXT)")
            db.execute("INSERT INTO data VALUES (?)", ("literal '; MATCH attack*",))
            db.commit()
            assert db.execute("SELECT value FROM data").fetchall() == [("literal '; MATCH attack*",)]
            flags = {name: db.getconfig(getattr(sqlite3, name)) for name in (
                "SQLITE_DBCONFIG_DEFENSIVE", "SQLITE_DBCONFIG_TRUSTED_SCHEMA",
                "SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION", "SQLITE_DBCONFIG_ENABLE_FTS3_TOKENIZER")}
            rejected = []
            for sql in ("CREATE VIRTUAL TABLE bad USING fts5(text)",
                        "ATTACH DATABASE ':memory:' AS bad", "PRAGMA trusted_schema=ON",
                        "PRAGMA writable_schema=ON", "SELECT load_extension('/tmp/bad')",
                        "SELECT fts3_tokenizer('simple')"):
                try:
                    db.execute(sql)
                except sqlite3.DatabaseError:
                    rejected.append(sql)
                else:
                    raise AssertionError("Unsafe SQL accepted: " + sql)
            try:
                db.load_extension("/nonexistent-extension")
            except sqlite3.OperationalError as error:
                assert "not authorized" in str(error), str(error)
            else:
                raise AssertionError("Native extension loading was enabled")
        finally:
            db.close()
        legacy = root / "legacy.sqlite3"
        with sqlite3.connect(legacy) as source:
            source.execute("CREATE VIRTUAL TABLE old_text USING fts5(text)")
            source.execute("INSERT INTO old_text VALUES ('legacy data preserved')")
        before = hashlib.sha256(legacy.read_bytes()).hexdigest()
        try:
            denied = connect(legacy)
        except ValueError:
            pass
        else:
            denied.close()
            raise AssertionError("Legacy virtual tables accepted for application SQL")
        check_integrity(legacy)
        assert before == hashlib.sha256(legacy.read_bytes()).hexdigest()
        assert not hasattr(sqlite3, "Session"), "Session API must be separately reviewed before use"
        return {"status": "sqlite_feature_restrictions_verified", "sqlite_version": sqlite3.sqlite_version,
                "config": flags, "rejected_sql": rejected, "extension_load_denied": True,
                "preexisting_virtual_schema_denied": True, "legacy_integrity_check_readonly": True,
                "legacy_content_unchanged": True, "python_session_api_present": False,
                "upstream_patched": False, "public_release_allowed": False, "real_model_requests": 0}


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
