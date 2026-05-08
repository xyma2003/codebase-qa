import os
from typing import Optional
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from indexer.chunker import CodeChunk
from config import CHROMA_DIR, EMBED_MODEL


def _get_collection_name(repo_url: str) -> str:
    """Derive a stable ChromaDB collection name from a repo URL."""
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    owner = repo_url.rstrip("/").split("/")[-2]
    # ChromaDB collection names must be 3-63 chars, alphanumeric + hyphens
    name = f"{owner}-{repo_name}".lower().replace("_", "-")[:63]
    return name


def _get_client() -> chromadb.PersistentClient:
    os.makedirs(CHROMA_DIR, exist_ok=True)
    return chromadb.PersistentClient(path=CHROMA_DIR)


def _get_embed_fn() -> SentenceTransformerEmbeddingFunction:
    return SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)


def is_indexed(repo_url: str) -> bool:
    """Check if a repo has already been indexed."""
    client = _get_client()
    name = _get_collection_name(repo_url)
    existing = [c.name for c in client.list_collections()]
    return name in existing


def index_chunks(chunks: list[CodeChunk], repo_url: str) -> int:
    """
    Store chunks in ChromaDB. Returns number of chunks indexed.
    Overwrites any existing collection for this repo.
    """
    client = _get_client()
    name = _get_collection_name(repo_url)

    # Delete existing collection if present (re-index)
    try:
        client.delete_collection(name)
    except Exception:
        pass

    collection = client.create_collection(
        name=name,
        embedding_function=_get_embed_fn(),
        metadata={"repo_url": repo_url},
    )

    # ChromaDB batch add (max 5461 per call due to sqlite limits)
    batch_size = 500
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i: i + batch_size]
        collection.add(
            ids=[f"{c.file_path}:{c.start_line}" for c in batch],
            documents=[c.content for c in batch],
            metadatas=[{
                "file_path": c.file_path,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "chunk_type": c.chunk_type,
                "name": c.name,
            } for c in batch],
        )

    return len(chunks)


def search(query: str, repo_url: str, n_results: int = 5) -> list[dict]:
    """
    Semantic search over indexed repo chunks.
    Returns list of dicts with keys: content, file_path, start_line, end_line, chunk_type, name.
    """
    client = _get_client()
    name = _get_collection_name(repo_url)
    collection = client.get_collection(name=name, embedding_function=_get_embed_fn())

    results = collection.query(query_texts=[query], n_results=n_results)

    output = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        output.append({
            "content": doc,
            "file_path": meta["file_path"],
            "start_line": meta["start_line"],
            "end_line": meta["end_line"],
            "chunk_type": meta["chunk_type"],
            "name": meta["name"],
        })
    return output


def list_files(repo_url: str, pattern: Optional[str] = None) -> list[str]:
    """List all indexed file paths. Optionally filter by substring pattern."""
    client = _get_client()
    name = _get_collection_name(repo_url)
    collection = client.get_collection(name=name, embedding_function=_get_embed_fn())

    # Get all unique file paths from metadata
    all_meta = collection.get(include=["metadatas"])["metadatas"]
    paths = sorted({m["file_path"] for m in all_meta})

    if pattern:
        paths = [p for p in paths if pattern.lower() in p.lower()]

    return paths
