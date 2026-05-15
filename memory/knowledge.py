"""
memory/knowledge.py — 知识库管理

每个仓库有一个独立的 memory 集合（{owner}-{repo}-memory），
存储高质量问答对，搜索时优先命中。
"""
import datetime
import uuid
from typing import Optional

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from config import CHROMA_DIR, EMBED_MODEL, MEMORY_SIMILARITY_THRESHOLD


def _get_client() -> chromadb.PersistentClient:
    import os
    os.makedirs(CHROMA_DIR, exist_ok=True)
    return chromadb.PersistentClient(path=CHROMA_DIR)


def _get_embed_fn() -> SentenceTransformerEmbeddingFunction:
    return SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)


def _memory_collection_name(repo_url: str) -> str:
    """memory 集合名 = 代码集合名 + '-memory'"""
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    owner = repo_url.rstrip("/").split("/")[-2]
    base = f"{owner}-{repo_name}".lower().replace("_", "-")[:55]
    return f"{base}-memory"


def _get_or_create_memory_collection(repo_url: str):
    client = _get_client()
    name = _memory_collection_name(repo_url)
    return client.get_or_create_collection(
        name=name,
        embedding_function=_get_embed_fn(),
        metadata={"repo_url": repo_url, "type": "memory"},
    )


def save_qa(question: str, answer: str, repo_url: str) -> str:
    """
    保存一条高质量问答到 memory 集合。
    文档内容 = "Q: {question}\nA: {answer}"，便于语义检索。
    返回新条目的 ID。
    """
    collection = _get_or_create_memory_collection(repo_url)
    doc_id = str(uuid.uuid4())
    document = f"Q: {question}\nA: {answer}"
    collection.add(
        ids=[doc_id],
        documents=[document],
        metadatas=[{
            "question": question,
            "answer": answer,
            "saved_at": datetime.datetime.now().isoformat(),
            "hit_count": 0,
            "source": "qa_pair",
        }],
    )
    return doc_id


def search_memory(query: str, repo_url: str) -> Optional[dict]:
    """
    在 memory 集合中语义搜索最相似的知识。
    返回 dict（含 question/answer/score）或 None（无命中或集合不存在）。
    """
    client = _get_client()
    name = _memory_collection_name(repo_url)

    # 集合不存在则直接返回
    existing = [c.name for c in client.list_collections()]
    if name not in existing:
        return None

    collection = client.get_collection(name=name, embedding_function=_get_embed_fn())

    # 集合为空时 query 会报错
    if collection.count() == 0:
        return None

    results = collection.query(
        query_texts=[query],
        n_results=1,
        include=["documents", "metadatas", "distances"],
    )

    if not results["ids"][0]:
        return None

    distance = results["distances"][0][0]
    # ChromaDB 返回 L2 距离，转换为相似度（近似）
    similarity = 1.0 / (1.0 + distance)

    if similarity < MEMORY_SIMILARITY_THRESHOLD:
        return None

    meta = results["metadatas"][0][0]
    doc_id = results["ids"][0][0]

    # 更新命中次数
    _increment_hit_count(collection, doc_id, meta)

    return {
        "question": meta["question"],
        "answer": meta["answer"],
        "score": round(similarity, 3),
        "saved_at": meta.get("saved_at", ""),
        "hit_count": meta.get("hit_count", 0) + 1,
    }


def _increment_hit_count(collection, doc_id: str, meta: dict):
    """更新命中次数（后台静默操作，失败不影响主流程）"""
    try:
        updated_meta = dict(meta)
        updated_meta["hit_count"] = meta.get("hit_count", 0) + 1
        collection.update(ids=[doc_id], metadatas=[updated_meta])
    except Exception:
        pass


def get_memory_stats(repo_url: str) -> dict:
    """返回知识库统计信息"""
    client = _get_client()
    name = _memory_collection_name(repo_url)
    existing = [c.name for c in client.list_collections()]

    if name not in existing:
        return {"count": 0, "latest": None}

    collection = client.get_collection(name=name, embedding_function=_get_embed_fn())
    count = collection.count()

    if count == 0:
        return {"count": 0, "latest": None}

    # 取最近一条的时间
    all_meta = collection.get(include=["metadatas"])["metadatas"]
    times = [m.get("saved_at", "") for m in all_meta if m.get("saved_at")]
    latest = max(times) if times else None

    return {"count": count, "latest": latest}
