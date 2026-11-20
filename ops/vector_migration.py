"""Import inert legacy exports into a new SQLite file, without Chroma installed."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

import numpy as np
from indexer.sqlite_vectors import LocalVectorClient, MAX_COLLECTIONS, MAX_RECORDS, name_value

MAX_EXPORT_BYTES = 256 * 1024 * 1024


def reject_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate migration JSON key")
        result[key] = value
    return result


def load_export(source, expected_sha256):
    source = Path(source)
    if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= MAX_EXPORT_BYTES:
        raise ValueError("Invalid migration export file")
    raw = source.read_bytes()
    if len(raw) > MAX_EXPORT_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Migration export checksum differs")
    data = json.loads(raw, object_pairs_hook=reject_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
    if not isinstance(data, dict) or set(data) != {"schema", "collections"} or data["schema"] != 1:
        raise ValueError("Unsupported migration export")
    rows = data["collections"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_COLLECTIONS:
        raise ValueError("Invalid migration collection count")
    names = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "metadata", "configuration", "records"}:
            raise ValueError("Invalid migration collection shape")
        name = name_value(row["name"])
        if name in names or not isinstance(row["records"], list) or len(row["records"]) > MAX_RECORDS:
            raise ValueError("Invalid migration records or repeated collection")
        names.add(name)
        config = row["configuration"]
        if not isinstance(config, dict) or not isinstance(config.get("hnsw"), dict) or config["hnsw"].get("space") not in {"l2", "cosine", "ip"}:
            raise ValueError("Migration must declare the original distance metric")
        # Only the application's old fixed embedding choice is accepted. The
        # dictionary is stored for comparison and is never used as a loader.
        embedding = config.get("embedding_function")
        if embedding is not None and (not isinstance(embedding, dict) or embedding.get("config") not in (
                {}, {"model_name": "all-MiniLM-L6-v2", "device": "cpu", "normalize_embeddings": False, "kwargs": {}})):
            raise ValueError("Unexpected legacy embedding configuration")
        for record in row["records"]:
            if not isinstance(record, dict) or set(record) != {"id", "document", "metadata", "embedding"}:
                raise ValueError("Invalid migration record shape")
    return data


def verify_export(client, data):
    if {x.name for x in client.list_collections()} != {x["name"] for x in data["collections"]}:
        raise ValueError("Migrated collection names differ")
    total = 0
    for row in data["collections"]:
        collection = client.get_collection(row["name"], embedding_function=None)
        if collection.metadata != row["metadata"] or collection.configuration_json != row["configuration"]:
            raise ValueError("Migrated collection binding differs")
        actual = collection.get(include=["embeddings", "documents", "metadatas"])
        by_id = {key: (text, meta, vector) for key, text, meta, vector in zip(actual["ids"], actual["documents"], actual["metadatas"], actual["embeddings"])}
        if len(by_id) != len(row["records"]):
            raise ValueError("Migrated vector count differs")
        for record in row["records"]:
            saved = by_id.get(record["id"])
            if saved is None or saved[:2] != (record["document"], record["metadata"]) or not np.array_equal(saved[2], np.asarray(record["embedding"], dtype="<f4")):
                raise ValueError("Migrated vector, text or metadata differs")
        total += len(by_id)
    return {"collections": len(data["collections"]), "vectors": total, "exact_float32_readback": True}


def import_export(source, destination, expected_sha256, *, service_stopped=False):
    if not service_stopped:
        raise ValueError("Stop the service before vector migration")
    data = load_export(source, expected_sha256)
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError("Migration destination cannot be a symlink")
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / "vectors.sqlite3"
    if final.exists() or final.is_symlink():
        raise FileExistsError("Vector destination already exists; it will not be overwritten")
    with tempfile.TemporaryDirectory(prefix=".vector-migration-", dir=destination) as temporary:
        client = LocalVectorClient(temporary)
        for row in data["collections"]:
            collection = client.create_collection(row["name"], metadata=row["metadata"], configuration=row["configuration"])
            for offset in range(0, len(row["records"]), 100):
                records = row["records"][offset:offset + 100]
                collection.add(ids=[x["id"] for x in records], documents=[x["document"] for x in records],
                    metadatas=[x["metadata"] for x in records], embeddings=[x["embedding"] for x in records])
        report = verify_export(client, data)
        with client.connect() as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Migrated SQLite integrity check failed")
        # link() publishes a complete file atomically and refuses concurrent
        # creation. No rename may overwrite a previous database.
        with client.path.open("rb") as file:
            os.fsync(file.fileno())
        os.link(client.path, final)
        client.path.unlink()
        directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return dict(report, source_sha256=expected_sha256, original_chroma_preserved=True,
        model_requests=0, embeddings_recomputed=0)
