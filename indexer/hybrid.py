"""Opt-in source-checked lexical and vector fusion, with fixed rank settings."""

import re
from typing import Literal

from indexer.code_search import find_in_snapshot
from indexer.snapshots import load_snapshot
from indexer.vector_store import search

RetrievalStrategy = Literal["semantic", "hybrid"]
RRF_CONSTANT = 60
MAX_QUERY_TOKENS = 8
LEXICAL_LIMIT = 20


def code_tokens(query):
    tokens = re.findall(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*(?![A-Za-z0-9_])", query)
    unique = []
    for token in tokens:
        if ("_" in token or token.endswith(".py") or re.search(r"[a-z][A-Z]", token)) and token not in unique:
            unique.append(token)
    return unique[:MAX_QUERY_TOKENS]


def fuse_ranks(dense, lexical, *, limit=5):
    items, scores, channels = {}, {}, {}
    for channel, values in (("semantic", dense), ("lexical", lexical)):
        seen = set()
        for item in values:
            key = (item["file_path"], item["start_line"])
            if key in seen:
                continue
            seen.add(key)
            rank = len(seen)
            items.setdefault(key, item)
            scores[key] = scores.get(key, 0) + 1/(RRF_CONSTANT+rank)
            channels.setdefault(key, []).append(channel)
    ordered = sorted(scores, key=lambda key: (-scores[key], key[0], key[1]))[:limit]
    return [dict(items[key], fusion_score=scores[key], retrieval_channels=channels[key]) for key in ordered]


def hybrid_search(repo_root, repo_url, commit_sha, query, collection_name):
    snapshot = load_snapshot(repo_root)
    if (snapshot.repo_url, snapshot.commit_sha) != (repo_url, commit_sha):
        raise ValueError("Hybrid retrieval source scope mismatch")
    dense = search(query, repo_url, 5, collection_name=collection_name)
    if any(hit.get("commit_sha") != commit_sha for hit in dense):
        raise ValueError("Hybrid vector results crossed source versions")
    lexical, seen = [], set()
    for token in code_tokens(query):
        mode = "path" if token.endswith(".py") else "symbol"
        result = find_in_snapshot(repo_root, repo_url, commit_sha, token, mode=mode, limit=LEXICAL_LIMIT)
        if not result.hits and "_" in token:
            result = find_in_snapshot(repo_root, repo_url, commit_sha, token, mode="text", limit=LEXICAL_LIMIT)
        for hit in result.hits:
            excerpt = snapshot.read(hit.file_path, hit.start_line or 1, hit.end_line)
            if excerpt.partial_last_line:
                if excerpt.start_line == excerpt.end_line:
                    continue
                excerpt = snapshot.read(hit.file_path, excerpt.start_line, excerpt.end_line-1)
            key = (hit.file_path, excerpt.start_line)
            if key in seen:
                continue
            seen.add(key)
            lexical.append({"file_path": hit.file_path, "start_line": excerpt.start_line, "end_line": excerpt.end_line,
                            "content": excerpt.content, "chunk_type": hit.kind, "name": hit.symbol or hit.file_path,
                            "commit_sha": commit_sha})
    return fuse_ranks(dense, lexical)
