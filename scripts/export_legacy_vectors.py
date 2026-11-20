"""Run ONLY with the archived Chroma environment, after stopping all writers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def legacy_client(source):
    # This is the sole optional legacy import. The serving environment does not
    # install Chroma. Never deserialize the stored embedding function.
    import chromadb
    from chromadb.config import Settings
    class LegacySettings(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
            return (init_settings,)
    settings = LegacySettings(_env_file=None, chroma_api_impl="chromadb.api.rust.RustBindingsAPI",
        is_persistent=True, persist_directory=str(source), allow_reset=False,
        anonymized_telemetry=False, chroma_otel_collection_endpoint="", chroma_otel_granularity=None)
    return chromadb.PersistentClient(path=str(source), settings=settings)


def export(source, output, *, service_stopped=False):
    from ops.backup import maintenance
    if not service_stopped:
        raise ValueError("Stop the service and all CLI writers before legacy export")
    source, output = Path(source).absolute(), Path(output).absolute()
    if source.is_symlink() or not (source / "chroma.sqlite3").is_file() or output.is_relative_to(source):
        raise ValueError("Invalid legacy source or export destination")
    with maintenance(source.parent):
        client = legacy_client(source)
        rows = []
        for item in sorted(client.list_collections(), key=lambda x: x.name):
            collection = client.get_collection(item.name, embedding_function=None)
            data = collection.get(include=["documents", "metadatas", "embeddings"])
            records = [{"id": identity, "document": text, "metadata": meta or {}, "embedding": vector.tolist()}
                for identity, text, meta, vector in zip(data["ids"], data["documents"], data["metadatas"], data["embeddings"])]
            rows.append({"name": collection.name, "metadata": collection.metadata or {}, "configuration": collection.configuration_json,
                "records": sorted(records, key=lambda x: x["id"])})
        raw = json.dumps({"schema": 1, "collections": rows}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > 256 * 1024 * 1024:
            raise ValueError("Legacy export exceeds limit")
        output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
    return {"sha256": hashlib.sha256(raw).hexdigest(), "collections": len(rows), "vectors": sum(len(x["records"]) for x in rows), "model_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service-stopped", action="store_true")
    args = parser.parse_args()
    print(json.dumps(export(args.source, args.output, service_stopped=args.service_stopped)))
