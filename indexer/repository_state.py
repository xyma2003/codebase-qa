"""Publish ready repository indexes with a single SQLite transaction."""

import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from config import CHROMA_DIR


@contextmanager
def _connect():
    path = Path(CHROMA_DIR) / "repository_state.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS versions (
            collection_name TEXT PRIMARY KEY,
            repo_url TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            record TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS active (
            repo_url TEXT PRIMARY KEY,
            collection_name TEXT NOT NULL
        );
    """)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def get_active(repo_url):
    with _connect() as db:
        row = db.execute(
            "SELECT v.record FROM active a JOIN versions v USING(collection_name) WHERE a.repo_url=?", (repo_url,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def list_versions(repo_url, fingerprint):
    with _connect() as db:
        rows = db.execute(
            "SELECT record FROM versions WHERE repo_url=? AND fingerprint=? ORDER BY created_at DESC",
            (repo_url, fingerprint),
        ).fetchall()
    unique = {}
    for row in rows:
        record = json.loads(row[0])
        unique.setdefault(record["commit_sha"], record)
    return list(unique.values())


def publish(record, expected_active):
    """An older concurrent build may be retained, but cannot replace a newer one."""
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT collection_name FROM active WHERE repo_url=?", (record["repo_url"],)).fetchone()
        current = row[0] if row else None
        db.execute(
            "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?)",
            (record["collection_name"], record["repo_url"], record["commit_sha"],
             record["fingerprint"], record["created_at"], json.dumps(record)),
        )
        if current == expected_active:
            db.execute("INSERT OR REPLACE INTO active VALUES (?, ?)", (record["repo_url"], record["collection_name"]))


def activate(record, expected_active):
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT collection_name FROM active WHERE repo_url=?", (record["repo_url"],)).fetchone()
        current = row[0] if row else None
        if current == expected_active:
            db.execute("INSERT OR REPLACE INTO active VALUES (?, ?)", (record["repo_url"], record["collection_name"]))
