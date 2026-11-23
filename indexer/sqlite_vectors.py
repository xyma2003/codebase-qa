"""Bounded local vector storage. JSON and float32 only; no component loading."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat

import numpy as np

from security.sqlite import connect

MAX_RECORDS = 10000
MAX_DIMENSIONS = 4096
MAX_TEXT_BYTES = 1024 * 1024
MAX_COLLECTION_BYTES = 64 * 1024 * 1024
MAX_COLLECTIONS = 1000
METRICS = {"cosine", "l2", "ip"}


def json_value(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text.encode()) > MAX_TEXT_BYTES:
        raise ValueError("Vector metadata exceeds limit")
    return text


def metadata_value(value):
    if not isinstance(value, dict) or not all(isinstance(k, str) and type(v) in (str, int, float, bool) for k, v in value.items()):
        raise ValueError("Vector metadata must contain scalar JSON values")
    json_value(value)
    return value


def vector_value(value):
    array = np.asarray(value, dtype="<f4")
    if array.ndim != 1 or not 1 <= len(array) <= MAX_DIMENSIONS or not np.isfinite(array).all():
        raise ValueError("Vector must be a bounded finite float32 array")
    return array


def name_value(value):
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= 128 or "\0" in value:
        raise ValueError("Invalid vector collection name")
    return value


class LocalVectorClient:
    def __init__(self, path):
        directory = Path(path)
        if directory.is_symlink():
            raise ValueError("Vector directory cannot be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "vectors.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Vector database must be a regular private file")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("Vector database must not be readable by other users")
        finally:
            os.close(fd)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported vector database schema")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS vector_collections (
                    name TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                    configuration TEXT NOT NULL, metric TEXT NOT NULL,
                    dimension INTEGER, identity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS vector_records (
                    collection TEXT NOT NULL REFERENCES vector_collections(name) ON DELETE CASCADE,
                    id TEXT NOT NULL, document TEXT NOT NULL, metadata TEXT NOT NULL,
                    embedding BLOB NOT NULL, PRIMARY KEY (collection, id));
            """)
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self):
        db = connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def list_collections(self):
        with self.connect() as db:
            names = [row[0] for row in db.execute("SELECT name FROM vector_collections ORDER BY name")]
        return [self.get_collection(name) for name in names]

    def create_collection(self, name, embedding_function=None, metadata=None, *, configuration=None):
        import uuid
        name_value(name)
        metadata = metadata_value(metadata or {})
        config = configuration or {"hnsw": {"space": "cosine" if embedding_function is not None else "l2"},
            "embedding_function": {"config": embedding_function.get_config()} if embedding_function is not None else None}
        metric = (config.get("hnsw") or {}).get("space", "l2")
        if metric not in METRICS:
            raise ValueError("Unsupported vector metric")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM vector_collections").fetchone()[0] >= MAX_COLLECTIONS:
                raise ValueError("Vector collection count exceeds limit")
            db.execute("INSERT INTO vector_collections VALUES(?,?,?,?,NULL,?)",
                (name, json_value(metadata), json_value(config), metric, uuid.uuid4().hex))
        return self.get_collection(name, embedding_function=embedding_function)

    def get_collection(self, name, embedding_function=None):
        name_value(name)
        with self.connect() as db:
            row = db.execute("SELECT metadata, configuration, metric, identity FROM vector_collections WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError("Vector collection not found")
        return LocalCollection(self, name, *row, embedding_function)

    def get_or_create_collection(self, name, embedding_function=None, metadata=None):
        try:
            return self.get_collection(name, embedding_function=embedding_function)
        except KeyError:
            try:
                return self.create_collection(name, embedding_function, metadata)
            except sqlite3.IntegrityError:
                return self.get_collection(name, embedding_function=embedding_function)

    def delete_collection(self, name):
        with self.connect() as db:
            if not db.execute("DELETE FROM vector_collections WHERE name=?", (name_value(name),)).rowcount:
                raise KeyError("Vector collection not found")

    def reset(self):
        raise PermissionError("Reset is disabled")


class LocalCollection:
    def __init__(self, client, name, metadata, configuration, metric, identity, embedding_function):
        self.client, self.name, self.metric, self.identity = client, name, metric, identity
        self.metadata = json.loads(metadata)
        self.configuration_json = json.loads(configuration)
        # Database configuration is data only. Only the application may provide
        # a callable, never a persisted component or remote model definition.
        self.embedding_function = embedding_function

    def _current(self, db):
        row = db.execute("SELECT dimension,identity FROM vector_collections WHERE name=?", (self.name,)).fetchone()
        if row is None or row[1] != self.identity:
            raise ValueError("Vector collection was replaced")
        return row[0]

    def count(self):
        with self.client.connect() as db:
            db.execute("BEGIN")
            self._current(db)
            return db.execute("SELECT count(*) FROM vector_records WHERE collection=?", (self.name,)).fetchone()[0]

    def _embed(self, documents):
        if self.embedding_function is None:
            raise ValueError("An explicit application embedding function is required")
        return self.embedding_function(input=documents)

    def add(self, ids, documents=None, metadatas=None, embeddings=None):
        self._write(ids, documents, metadatas, embeddings, update=False)

    def update(self, ids, documents=None, metadatas=None, embeddings=None):
        self._write(ids, documents, metadatas, embeddings, update=True)

    def _write(self, ids, documents, metadatas, embeddings, *, update):
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_RECORDS or len(set(ids)) != len(ids):
            raise ValueError("Vector IDs must be unique and bounded")
        if any(not isinstance(x, str) or not 1 <= len(x.encode()) <= 1024 for x in ids):
            raise ValueError("Invalid vector ID")
        for field in (documents, metadatas, embeddings):
            if field is not None and len(field) != len(ids):
                raise ValueError("Vector batch lengths differ")
        if documents is not None and any(not isinstance(x, str) or len(x.encode()) > MAX_TEXT_BYTES for x in documents):
            raise ValueError("Invalid or oversized vector document")
        if metadatas is not None:
            for value in metadatas:
                metadata_value(value)
        if embeddings is None and documents is not None:
            embeddings = self._embed(documents)
        if not update and (documents is None or embeddings is None):
            raise ValueError("Vector additions require documents and embeddings")
        if embeddings is not None:
            if len(embeddings) != len(ids):
                raise ValueError("Embedding output length differs")
            embeddings = [vector_value(value) for value in embeddings]
        with self.client.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            dimension = self._current(db)
            if embeddings is not None:
                dimension = dimension or len(embeddings[0])
                if any(len(v) != dimension for v in embeddings):
                    raise ValueError("Vector dimension differs")
                db.execute("UPDATE vector_collections SET dimension=? WHERE name=?", (dimension, self.name))
            for index, identity in enumerate(ids):
                old = db.execute("SELECT document,metadata,embedding FROM vector_records WHERE collection=? AND id=?", (self.name, identity)).fetchone()
                if update and old is None:
                    raise KeyError("Updated vector not found")
                if not update and old is not None:
                    raise ValueError("Vector ID already exists")
                document = documents[index] if documents is not None else old[0]
                meta = json.loads(old[1]) if old is not None else {}
                if metadatas is not None:
                    meta.update(metadatas[index])
                vector = embeddings[index].tobytes() if embeddings is not None else old[2]
                db.execute("INSERT INTO vector_records VALUES(?,?,?,?,?) ON CONFLICT(collection,id) DO UPDATE SET document=excluded.document,metadata=excluded.metadata,embedding=excluded.embedding",
                    (self.name, identity, document, json_value(meta), vector))
            total, size = db.execute("SELECT count(*),coalesce(sum(length(CAST(document AS BLOB))+length(CAST(metadata AS BLOB))+length(embedding)),0) FROM vector_records WHERE collection=?", (self.name,)).fetchone()
            if total > MAX_RECORDS or size > MAX_COLLECTION_BYTES:
                raise ValueError("Vector collection exceeds storage limit")

    def _rows(self, ids=None, where=None):
        if ids is not None and (not isinstance(ids, list) or len(ids) > MAX_RECORDS or any(not isinstance(x, str) for x in ids)):
            raise ValueError("Invalid vector IDs")
        if where is not None:
            metadata_value(where)
        with self.client.connect() as db:
            db.execute("BEGIN")
            dimension = self._current(db)
            values = db.execute("SELECT id,document,metadata,embedding FROM vector_records WHERE collection=? ORDER BY rowid", (self.name,)).fetchall()
        selected = set(ids) if ids is not None else None
        rows = []
        for identity, document, raw_meta, raw_vector in values:
            meta = json.loads(raw_meta)
            if (selected is not None and identity not in selected) or (where is not None and any(meta.get(k) != v for k, v in where.items())):
                continue
            vector = vector_value(np.frombuffer(raw_vector, dtype="<f4"))
            if len(vector) != dimension:
                raise ValueError("Stored vector dimension differs")
            rows.append((identity, document, meta, vector))
        return rows

    def get(self, ids=None, include=None, where=None, limit=None, offset=0):
        if type(offset) is not int or offset < 0 or (limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_RECORDS)):
            raise ValueError("Invalid vector pagination")
        include = ["documents", "metadatas"] if include is None else include
        if not isinstance(include, list) or not set(include) <= {"documents", "metadatas", "embeddings"}:
            raise ValueError("Unsupported vector fields")
        rows = self._rows(ids, where)[offset:None if limit is None else offset + limit]
        result = {"ids": [row[0] for row in rows], "documents": None, "metadatas": None, "embeddings": None}
        for key, index in (("documents", 1), ("metadatas", 2), ("embeddings", 3)):
            if key in include:
                result[key] = np.array([row[index] for row in rows], dtype=np.float32) if key == "embeddings" else [row[index] for row in rows]
        return result

    def query(self, query_texts=None, query_embeddings=None, n_results=5, where=None):
        if type(n_results) is not int or not 1 <= n_results <= MAX_RECORDS or (query_texts is None) == (query_embeddings is None):
            raise ValueError("Invalid vector query")
        if query_texts is not None:
            if not isinstance(query_texts, list) or not 1 <= len(query_texts) <= 100 or any(not isinstance(x, str) or len(x.encode()) > MAX_TEXT_BYTES for x in query_texts):
                raise ValueError("Invalid vector query texts")
            query_embeddings = self._embed(query_texts)
        if not 1 <= len(query_embeddings) <= 100:
            raise ValueError("Invalid vector query count")
        rows = self._rows(where=where)
        output = {key: [] for key in ("ids", "documents", "metadatas", "distances")}
        # Float64 arithmetic avoids overflow for valid float32 inputs; storage
        # and embedding outputs remain byte-identical float32.
        matrix = np.array([row[3] for row in rows], dtype=np.float64)
        for value in query_embeddings:
            vector = vector_value(value).astype(np.float64)
            if rows:
                if matrix.shape[1] != len(vector):
                    raise ValueError("Vector query dimension differs")
                if self.metric == "l2":
                    distances = np.sum((matrix - vector) ** 2, axis=1)
                elif self.metric == "ip":
                    distances = 1 - matrix @ vector
                else:
                    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(vector)
                    distances = 1 - np.divide(matrix @ vector, norms, out=np.zeros(len(rows), dtype=np.float64), where=norms != 0)
                order = np.argsort(distances, kind="stable")[:n_results]
            else:
                distances, order = [], []
            for key, index in (("ids", 0), ("documents", 1), ("metadatas", 2)):
                output[key].append([rows[int(i)][index] for i in order])
            output["distances"].append([float(distances[int(i)]) for i in order])
        return output
