import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from security.sqlite import connect, check_integrity, _integrity_authorizer


class SQLiteSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def connection(self, name="plain.sqlite3", **kwargs):
        db = connect(self.root / name, **kwargs)
        self.addCleanup(db.close)
        return db

    def test_plain_tables_and_transactions_preserve_literal_untrusted_text(self):
        db = self.connection()
        value = "'); CREATE VIRTUAL TABLE bad USING fts5(text); --"
        with db:
            db.execute("CREATE TABLE data (value TEXT)")
            db.execute("INSERT INTO data VALUES (?)", (value,))
            db.execute("PRAGMA user_version=1")
        self.assertEqual(db.execute("SELECT value FROM data").fetchall(), [(value,)])
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (1,))

    def test_native_defensive_and_extension_flags_are_effective(self):
        db = self.connection()
        for flag, expected in ((sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True),
                               (sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False),
                               (sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, False),
                               (sqlite3.SQLITE_DBCONFIG_ENABLE_FTS3_TOKENIZER, False)):
            self.assertEqual(db.getconfig(flag), expected)
        with self.assertRaises(sqlite3.OperationalError):
            db.load_extension("/nonexistent-extension")

    def test_virtual_tables_attachment_and_unsafe_pragmas_are_denied(self):
        db = self.connection()
        for sql in ("CREATE VIRTUAL TABLE bad USING fts5(text)",
                    "CREATE VIRTUAL TABLE bad USING fts4(text)",
                    "ATTACH DATABASE ':memory:' AS extra", "DETACH DATABASE extra",
                    "PRAGMA writable_schema=ON", "PRAGMA trusted_schema=ON",
                    "PRAGMA temp_store_directory='/tmp'", "SELECT load_extension('/tmp/bad')",
                    "SELECT fts3_tokenizer('simple')", "SELECT fts5('bad')"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                db.execute(sql)

    def legacy(self):
        path = self.root / "chroma.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE VIRTUAL TABLE old_text USING fts5(text)")
            db.execute("INSERT INTO old_text VALUES ('legacy preserved')")
        return path

    def test_preexisting_virtual_schema_is_rejected_without_modifying_file(self):
        path = self.legacy()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "虚表"):
            connect(path)
        self.assertEqual(path.read_bytes(), before)

    def test_retained_legacy_fts_has_full_readonly_integrity_check(self):
        path = self.legacy()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        check_integrity(path)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT text FROM old_text").fetchall(), [("legacy preserved",)])

    def test_integrity_policy_rejects_writes_match_and_attachment(self):
        path = self.legacy()
        db = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)
        self.addCleanup(db.close)
        db.set_authorizer(_integrity_authorizer)
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        for sql in ("DELETE FROM old_text", "INSERT INTO old_text VALUES ('bad')",
                    "SELECT * FROM old_text WHERE old_text MATCH 'legacy*'",
                    "ATTACH DATABASE ':memory:' AS extra", "PRAGMA writable_schema=ON"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                db.execute(sql)

    def test_corrupt_file_is_rejected_and_never_rewritten(self):
        path = self.root / "bad.sqlite3"
        path.write_bytes(b"corrupt database fixture")
        with self.assertRaises(sqlite3.DatabaseError):
            check_integrity(path)
        self.assertEqual(path.read_bytes(), b"corrupt database fixture")

    def test_readonly_business_connection_cannot_write(self):
        db = self.connection()
        db.execute("CREATE TABLE data (value TEXT)")
        db.commit()
        readonly = self.connection(read_only=True)
        self.assertEqual(readonly.execute("SELECT * FROM data").fetchall(), [])
        with self.assertRaises(sqlite3.OperationalError):
            readonly.execute("INSERT INTO data VALUES ('bad')")


if __name__ == "__main__":
    unittest.main()
