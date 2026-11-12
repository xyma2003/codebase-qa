"""Keep source, search collection and repository version bound together."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from git import Repo
from config import CODE_EXTENSIONS, EMBED_MODEL, MAX_CHUNK_TOKENS
from indexer.cloner import clone_repo
from indexer.chunker import chunk_repo
from indexer import repository_state as state
from indexer import vector_store
from indexer.snapshots import create_snapshot, load_snapshot, normalize_repo_url
from security.sources import safe_chunks


@dataclass(frozen=True)
class RepositoryIndex:
    repo_url: str
    commit_sha: str
    repo_root: str
    collection_name: str
    chunk_count: int
    file_count: int
    created_at: str
    fingerprint: str
    reused_legacy_vectors: bool = False

    @classmethod
    def from_record(cls, record):
        result = cls(**record)
        snapshot = load_snapshot(result.repo_root)
        if snapshot.repo_url != result.repo_url or snapshot.commit_sha != result.commit_sha:
            raise ValueError("索引与代码快照版本不一致。")
        collection = vector_store._get_client().get_collection(result.collection_name, embedding_function=None)
        metadata = collection.metadata or {}
        if metadata.get("repo_url") != result.repo_url or metadata.get("commit_sha") != result.commit_sha or collection.count() != result.chunk_count:
            raise ValueError("索引存储与所选代码版本不一致。")
        return result


def _fingerprint():
    settings = {
        "embedding_model": EMBED_MODEL, "max_chunk_tokens": MAX_CHUNK_TOKENS,
        "extensions": sorted(CODE_EXTENSIONS),
        "chunker_sha256": hashlib.sha256(Path(__file__).with_name("chunker.py").read_bytes()).hexdigest(),
    }
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def get_current_repository(repo_url):
    record = state.get_active(normalize_repo_url(repo_url))
    if record and record["fingerprint"] == _fingerprint():
        if record["collection_name"] not in {collection.name for collection in vector_store._get_client().list_collections()}:
            return None
        return RepositoryIndex.from_record(record)
    return None


def list_repository_versions(repo_url):
    rows = state.list_versions(normalize_repo_url(repo_url), _fingerprint())
    if not rows:
        return []
    available = {collection.name for collection in vector_store._get_client().list_collections()}
    return [RepositoryIndex.from_record(row) for row in rows if row["collection_name"] in available]


def get_repository_version(repo_url, commit_sha):
    for version in list_repository_versions(repo_url):
        if version.commit_sha == commit_sha:
            return version
    raise ValueError("该代码版本尚未成功索引。")


def _fetch_default_commit(checkout):
    with Repo(checkout) as repo:
        repo.git.fetch("--depth=1", "origin", "HEAD")
        return repo.commit("FETCH_HEAD").hexsha


def _legacy_embeddings(client, repo_url, chunks):
    """Reuse old vectors only after full source and embedding-config comparison."""
    name = vector_store._get_collection_name(repo_url)
    if name not in {collection.name for collection in client.list_collections()}:
        return None
    collection = client.get_collection(name=name, embedding_function=None)
    config = (collection.configuration_json.get("embedding_function") or {}).get("config")
    try:
        source_url = normalize_repo_url((collection.metadata or {}).get("repo_url", ""))
    except ValueError:
        return None
    if config != vector_store._get_embed_fn().get_config() or source_url != repo_url:
        return None
    stored = collection.get(include=["documents", "metadatas", "embeddings"])
    expected = {f"{chunk.file_path}:{chunk.start_line}": chunk for chunk in chunks}
    if len(expected) != len(chunks) or set(stored["ids"]) != set(expected) or stored["embeddings"] is None:
        return None
    vectors = {}
    for identity, document, metadata, embedding in zip(
        stored["ids"], stored["documents"], stored["metadatas"], stored["embeddings"]
    ):
        chunk = expected[identity]
        if document != chunk.content or any(metadata.get(key) != getattr(chunk, key) for key in ("file_path", "start_line", "end_line", "chunk_type", "name")):
            return None
        vectors[identity] = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
    return [vectors[f"{chunk.file_path}:{chunk.start_line}"] for chunk in chunks]


def import_repository(repo_url, refresh=False, commit_sha=None, progress_callback=None):
    repo_url = normalize_repo_url(repo_url)
    if commit_sha:
        if refresh:
            raise ValueError("选择已有版本时不能同时更新远端。")
        return get_repository_version(repo_url, commit_sha)
    current = get_current_repository(repo_url)
    if current and not refresh:
        return current
    previous = state.get_active(repo_url)
    checkout = clone_repo(repo_url)
    with Repo(checkout) as repo:
        revision = _fetch_default_commit(checkout) if refresh else (previous["commit_sha"] if previous else repo.head.commit.hexsha)
    snapshot = create_snapshot(repo_url, checkout, revision)
    for version in list_repository_versions(repo_url):
        if version.commit_sha == snapshot.commit_sha:
            state.activate(vars(version), previous["collection_name"] if previous else None)
            return version
    chunks = safe_chunks(chunk_repo(snapshot.root), snapshot)
    if not chunks:
        raise ValueError("仓库中没有支持索引的代码或文档。")
    client = vector_store._get_client()
    embeddings = _legacy_embeddings(client, repo_url, chunks)
    identity = hashlib.sha256(repo_url.encode()).hexdigest()[:16]
    name = f"repo-{identity}-{snapshot.commit_sha[:12]}-{uuid.uuid4().hex[:12]}"
    record = dict(
        repo_url=repo_url, commit_sha=snapshot.commit_sha, repo_root=snapshot.root,
        collection_name=name, chunk_count=len(chunks),
        file_count=len({chunk.file_path for chunk in chunks}),
        created_at=datetime.now(timezone.utc).isoformat(), fingerprint=_fingerprint(),
        reused_legacy_vectors=embeddings is not None,
    )
    try:
        vector_store.build_collection(chunks, repo_url, name, snapshot.commit_sha, embeddings, progress_callback)
        result = RepositoryIndex.from_record(record)
        state.publish(record, previous["collection_name"] if previous else None)
    except Exception:
        # Only this attempt's unpublished, uniquely named collection is disposable.
        try:
            if (state.get_active(repo_url) or {}).get("collection_name") != name and name in {collection.name for collection in client.list_collections()}:
                client.delete_collection(name)
        except Exception:
            pass  # Preserve the original failure; unknown publication state is not disposable.
        raise
    return result
