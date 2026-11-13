"""Version-bound answer candidates; only reviewed, rechecked entries are retrieved."""

from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from config import CHROMA_DIR, EMBED_MODEL, MEMORY_SIMILARITY_THRESHOLD
from indexer.snapshots import load_snapshot, normalize_repo_url
from indexer.citations import SourceCitation, check_source_citation
from security.sources import ensure_safe_text, MAX_SOURCE_BYTES
from security.identity import current_principal, AccessDenied

MAX_CACHE_ENTRIES = 2000


def _get_client():
    os.makedirs(CHROMA_DIR, exist_ok=True)
    return chromadb.PersistentClient(path=CHROMA_DIR)


def _get_embed_fn():
    return SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)


def _memory_collection_name(repo_url, commit_sha=None):
    url = normalize_repo_url(repo_url)
    owner_id = current_principal().owner_id
    if owner_id != "local":
        identity = hashlib.sha256(f"{owner_id}\0{url}\0{commit_sha or ''}".encode()).hexdigest()
        return f"private-memory-{identity[:48]}"
    owner, name = url.rsplit("/", 2)[-2:]
    base = f"{owner}-{name}".lower().replace("_", "-")[:55]
    if commit_sha:
        identity = hashlib.sha256(f"{url}@{commit_sha}".encode()).hexdigest()[:24]
        return f"{base[:30]}-m-{identity}"
    return f"{base}-memory"


def _collection(repo_url, commit_sha, *, create=False):
    name = _memory_collection_name(repo_url, commit_sha)
    client = _get_client()
    if create:
        collection = client.get_or_create_collection(name=name, embedding_function=_get_embed_fn(),
            metadata={"repo_url": normalize_repo_url(repo_url), "type": "memory", "commit_sha": commit_sha,
                      "owner_id":current_principal().owner_id})
    else:
        if name not in {item.name for item in client.list_collections()}:
            return None
        collection = client.get_collection(name=name, embedding_function=None)
    if (collection.metadata or {}).get("owner_id", "local") != current_principal().owner_id:
        raise AccessDenied("历史答案不存在或没有访问权限。")
    return collection


@contextmanager
def _lock(repo_url, commit_sha):
    name = _memory_collection_name(repo_url, commit_sha)
    directory = Path(CHROMA_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory/(name+".review.lock"),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _document(meta):
    return f"Q: {meta['question']}\nA: {meta['answer']}"


def _binding(meta):
    keys = ("question", "answer", "repo_url", "commit_sha", "citations_json", "cache_format")
    return _digest({key:meta[key] for key in (*keys, "owner_id") if key in meta})


def _review_token(document, meta):
    return _digest({"document": document, "metadata": {key: value for key, value in meta.items() if key != "hit_count"}})


def _validate(meta, document, repo_url, commit_sha, repo_root):
    if meta.get("owner_id", "local") != current_principal().owner_id:
        raise AccessDenied("历史答案不存在或没有访问权限。")
    if (meta.get("cache_format") != 1 or meta.get("repo_url") != normalize_repo_url(repo_url)
            or meta.get("commit_sha") != commit_sha or meta.get("content_sha256") != _binding(meta)
            or document != _document(meta)):
        raise ValueError("历史答案缺少有效版本、来源或内容绑定，需要重新生成。")
    if len(document.encode()) > MAX_SOURCE_BYTES:
        raise ValueError("历史答案超过内容上限。")
    ensure_safe_text(document)
    snapshot = load_snapshot(repo_root)
    if (snapshot.repo_url, snapshot.commit_sha) != (normalize_repo_url(repo_url), commit_sha):
        raise ValueError("历史答案与当前源码版本不一致。")
    citations = json.loads(meta["citations_json"])
    if not isinstance(citations, list) or not 1 <= len(citations) <= 50:
        raise ValueError("历史答案缺少可核对依据。")
    for value in citations:
        source = SourceCitation.model_validate(value)
        if (source.repo_url, source.commit_sha) != (snapshot.repo_url, snapshot.commit_sha):
            raise ValueError("历史答案引用跨越了仓库或版本。")
        check_source_citation(snapshot.root, **source.model_dump())
    return citations


def save_qa(question, answer, repo_url, *, commit_sha=None, citations=()):
    if not commit_sha or not citations:
        raise ValueError("缓存候选需要固定版本和源码依据。")
    ensure_safe_text(question)
    ensure_safe_text(answer)
    sources = []
    for citation in citations:
        source = SourceCitation.model_validate(citation.model_dump(exclude={"reference_id"}))
        if (source.repo_url, source.commit_sha) != (normalize_repo_url(repo_url), commit_sha):
            raise ValueError("缓存依据与当前版本不一致。")
        ensure_safe_text(source.source_text)
        sources.append(source.model_dump())
    meta = {"question": question, "answer": answer, "repo_url": normalize_repo_url(repo_url), "commit_sha": commit_sha,
            "owner_id": current_principal().owner_id,
            "cache_format": 1, "citations_json": json.dumps(sources, ensure_ascii=False, sort_keys=True),
            "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "hit_count": 0,
            "source": "qa_pair", "approval_status": "pending"}
    document = _document(meta)
    if len(document.encode()) > MAX_SOURCE_BYTES or not 1 <= len(sources) <= 50:
        raise ValueError("缓存候选超过内容或依据上限。")
    meta["content_sha256"] = _binding(meta)
    identity = str(uuid.uuid4())
    with _lock(repo_url, commit_sha):
        collection = _collection(repo_url, commit_sha, create=True)
        if collection.count() >= MAX_CACHE_ENTRIES:
            raise ValueError("历史答案数量达到安全上限。")
        collection.add(ids=[identity], documents=[document], metadatas=[meta])
    return identity


def search_memory(query, repo_url, *, commit_sha=None, repo_root=None):
    if not commit_sha or not repo_root:
        return None
    ensure_safe_text(query)
    with _lock(repo_url, commit_sha):
        collection = _collection(repo_url, commit_sha)
        if collection is None or not collection.get(where={"approval_status": "approved"}, limit=1, include=[])["ids"]:
            return None
        collection = _get_client().get_collection(name=collection.name, embedding_function=_get_embed_fn())
        result = collection.query(query_texts=[query], n_results=1, where={"approval_status": "approved"})
        if not result["ids"][0]:
            return None
        meta, document = result["metadatas"][0][0], result["documents"][0][0]
        try:
            _validate(meta, document, repo_url, commit_sha, repo_root)
            if meta.get("approved_sha256") != meta["content_sha256"]:
                return None
        except (ValueError, KeyError, TypeError, OSError):
            return None
        score = 1.0/(1.0 + result["distances"][0][0])
        if score < MEMORY_SIMILARITY_THRESHOLD:
            return None
        collection.update(ids=[result["ids"][0][0]], metadatas=[{"hit_count": meta.get("hit_count", 0)+1}])
        return {"question": meta["question"], "answer": meta["answer"], "score": round(score, 3),
                "hit_count": meta.get("hit_count", 0)+1, "saved_at": meta["saved_at"], "approval_status": "approved"}


def list_cache(repo_url, commit_sha, *, limit=20, offset=0):
    if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
        raise ValueError("缓存分页参数无效。")
    collection = _collection(repo_url, commit_sha)
    if collection is None:
        return []
    data = collection.get(limit=limit, offset=offset, include=["documents", "metadatas"])
    return [{"id": identity, "document": document, "metadata": meta, "review_token": _review_token(document, meta)}
            for identity, document, meta in zip(data["ids"], data["documents"], data["metadatas"])
            if meta.get("owner_id", "local") == current_principal().owner_id]


def review_qa(repo_url, commit_sha, repo_root, identity, expected_token, *, approve):
    if type(approve) is not bool:
        raise ValueError("审核动作无效。")
    with _lock(repo_url, commit_sha):
        collection = _collection(repo_url, commit_sha)
        if collection is None:
            raise ValueError("历史答案不存在。")
        data = collection.get(ids=[identity], include=["documents", "metadatas"])
        if len(data["ids"]) != 1:
            raise ValueError("历史答案不存在。")
        meta, document = data["metadatas"][0], data["documents"][0]
        if meta.get("owner_id", "local") != current_principal().owner_id:
            raise AccessDenied("历史答案不存在或没有访问权限。")
        if _review_token(document, meta) != expected_token:
            raise ValueError("历史答案已被修改，请重新打开后审核。")
        if approve:
            _validate(meta, document, repo_url, commit_sha, repo_root)
        collection.update(ids=[identity], metadatas=[{"approval_status": "approved" if approve else "revoked",
            "approved_sha256": meta["content_sha256"] if approve else "",
            "reviewed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}])


def get_memory_stats(repo_url, *, commit_sha=None):
    collection = _collection(repo_url, commit_sha)
    if collection is None:
        return {"count": 0, "latest": None, "total_count": 0, "pending_count": 0}
    data = [meta for meta in collection.get(limit=MAX_CACHE_ENTRIES, include=["metadatas"])["metadatas"]
            if meta.get("owner_id", "local") == current_principal().owner_id]
    approved = sum(meta.get("approval_status") == "approved" for meta in data)
    pending = sum(meta.get("approval_status", "pending") == "pending" for meta in data)
    return {"count": approved, "latest": max((meta.get("saved_at", "") for meta in data), default=None),
            "total_count": len(data), "pending_count": pending}
