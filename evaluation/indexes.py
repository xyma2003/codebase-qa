"""Explicit pinned evaluation indexes, without publishing a new app default."""

from pathlib import Path
from uuid import uuid4

from git import Repo

from evaluation.archive import write_json
from evaluation.dataset import _blob, digest, read_json, validate_bundle
from indexer import vector_store
from indexer.chunker import chunk_repo
from indexer.repository_service import _fingerprint
from indexer.snapshots import create_snapshot, load_snapshot


def _contents(collection):
    data = collection.get(include=["documents", "metadatas", "embeddings"])
    vectors = data["embeddings"]
    if vectors is None:
        raise ValueError("评测索引缺少向量")
    return sorted([{"id": identity, "text": text, "metadata": meta,
                    "embedding_sha256": digest(vector.tobytes())}
                   for identity, text, meta, vector in zip(data["ids"], data["documents"], data["metadatas"], vectors)],
                  key=lambda row: row["id"])


def build_indexes(dataset, repository, output):
    bundle = validate_bundle(dataset, repository)
    destination = Path(output).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    for source in bundle.manifest.sources:
        snapshot = create_snapshot(source.repo_url, str(repository), source.commit_sha, storage_dir=destination / "sources")
        chunks = list(chunk_repo(snapshot.root))
        if not chunks:
            raise ValueError("固定版本没有可索引内容")
        name = "evaluation-" + uuid4().hex
        vector_store.build_collection(chunks, source.repo_url, name, source.commit_sha)
        collection = vector_store._get_client().get_collection(name, embedding_function=None)
        records.append({"source_id": source.source_id, "repo_url": source.repo_url, "commit_sha": source.commit_sha,
                        "repo_root": snapshot.root, "collection_name": name, "chunk_count": len(chunks),
                        "fingerprint": _fingerprint(), "chunks": _contents(collection)})
    manifest = {"format_version": 1, "dataset_fingerprint": bundle.fingerprint, "sources": records}
    write_json(destination / "index.json", manifest)
    check_indexes(destination, bundle, repository)
    return {"status": "evaluation_indexes_ready", "dataset_fingerprint": bundle.fingerprint,
            "source_count": len(records), "chunk_count": sum(item["chunk_count"] for item in records),
            "manifest_sha256": digest((destination / "index.json").read_bytes())}


def check_index_manifest(manifest, bundle, repository):
    sources = {source.source_id: source for source in bundle.manifest.sources}
    if (manifest.get("format_version") != 1 or manifest.get("dataset_fingerprint") != bundle.fingerprint
            or len(manifest["sources"]) != len(sources) or {row["source_id"] for row in manifest["sources"]} != set(sources)):
        raise ValueError("评测索引与固定数据集不一致")
    with Repo(repository) as repo:
        for row in manifest["sources"]:
            source = sources[row["source_id"]]
            if (row["repo_url"], row["commit_sha"]) != (source.repo_url, source.commit_sha):
                raise ValueError("评测索引代码版本不一致")
            chunks = row["chunks"]
            if row["chunk_count"] != len(chunks) or len({item["id"] for item in chunks}) != len(chunks) or not chunks:
                raise ValueError("评测索引内容不完整")
            commit = repo.commit(source.commit_sha)
            for item in chunks:
                meta = item["metadata"]
                data = _blob(commit, meta["file_path"])
                lines = data.decode("utf-8", errors="ignore").splitlines()
                if (meta["commit_sha"] != source.commit_sha or not 1 <= meta["start_line"] <= meta["end_line"] <= len(lines)
                        or item["text"] != "\n".join(lines[meta["start_line"]-1:meta["end_line"]])
                        or item["id"] != f"{meta['file_path']}:{meta['start_line']}"):
                    raise ValueError("评测索引原文或行号不匹配")


def check_indexes(directory, bundle, repository):
    manifest = read_json((Path(directory) / "index.json").read_bytes())
    check_index_manifest(manifest, bundle, repository)
    for row in manifest["sources"]:
        snapshot = load_snapshot(row["repo_root"], verify=True)
        if (snapshot.repo_url, snapshot.commit_sha) != (row["repo_url"], row["commit_sha"]) or row["fingerprint"] != _fingerprint():
            raise ValueError("评测快照或分块、嵌入配置已变化")
        collection = vector_store._get_client().get_collection(row["collection_name"], embedding_function=None)
        if ((collection.metadata or {}) != {"repo_url": row["repo_url"], "commit_sha": row["commit_sha"]}
                or _contents(collection) != row["chunks"]):
            raise ValueError("实际向量存储与固定索引清单不一致")
    return manifest
