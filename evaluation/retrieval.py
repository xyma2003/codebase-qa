"""One-variable offline retrieval comparison; no model answers or cache changes."""

from dataclasses import asdict
import json
from pathlib import Path
import time
from uuid import uuid4
import numpy as np

from evaluation.archive import write_json
from evaluation.dataset import digest, validate_bundle
from evaluation.indexes import check_indexes
from indexer import vector_store
from indexer.chunker import CodeChunk
from indexer.module_chunks import module_supplements
from indexer.snapshots import load_snapshot

ROOT = Path(__file__).resolve().parents[1]


def evidence_coverage(hits, evidence):
    spans = sorted((max(hit["start_line"], evidence.start_line), min(hit["end_line"], evidence.end_line))
                   for hit in hits if hit["file_path"] == evidence.file_path and hit["start_line"] <= evidence.end_line and hit["end_line"] >= evidence.start_line)
    cursor = evidence.start_line
    for start, end in spans:
        if start > cursor:
            break
        cursor = max(cursor, end + 1)
    return {"overlap": bool(spans), "full_coverage": cursor > evidence.end_line}


def group_coverage(hits, groups, evidence):
    return [{"group_id": group.group_id,
             **{metric: any(evidence_coverage(hits, evidence[identity])[metric] for identity in group.any_of)
                for metric in ("overlap", "full_coverage")}} for group in groups]


def _summary(turns):
    included = [turn for turn in turns if turn["evidence_required"]]
    groups = [group for turn in included for group in turn["groups"]]
    return {"required_turns": len(included), "required_groups": len(groups),
            "overlap_groups": sum(group["overlap"] for group in groups),
            "fully_covered_groups": sum(group["full_coverage"] for group in groups),
            "group_overlap_recall": sum(group["overlap"] for group in groups)/len(groups) if groups else None,
            "group_full_coverage_recall": sum(group["full_coverage"] for group in groups)/len(groups) if groups else None}


def compare_module_chunks(dataset, repository, index_directory, output, *, candidate_collections=None):
    bundle = validate_bundle(dataset, repository)
    baseline = check_indexes(index_directory, bundle, repository)
    destination = Path(output).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    variants = {"baseline": {}, "module_supplement": {}}
    counts = []
    for source in baseline["sources"]:
        name = source["collection_name"]
        snapshot = load_snapshot(source["repo_root"], verify=True)
        data = vector_store._get_client().get_collection(name, embedding_function=None).get(include=["documents", "metadatas", "embeddings"])
        chunks = [CodeChunk(text, meta["file_path"], meta["start_line"], meta["end_line"], meta["chunk_type"], meta["name"])
                  for text, meta in zip(data["documents"], data["metadatas"])]
        additions = module_supplements(snapshot, chunks)
        candidate = (candidate_collections or {}).get(source["source_id"])
        reused_candidate = bool(candidate)
        if candidate is None:
            embeddings = data["embeddings"].tolist()
            if additions:
                extra = vector_store._get_embed_fn()(input=[chunk.content for chunk in additions])
                embeddings.extend([vector.tolist() if hasattr(vector, "tolist") else list(vector) for vector in extra])
            candidate = "retrieval-module-" + uuid4().hex
            vector_store.build_collection(chunks + additions, source["repo_url"], candidate, source["commit_sha"], embeddings)
        collection = vector_store._get_client().get_collection(candidate, embedding_function=None)
        after = collection.get(include=["documents", "metadatas", "embeddings"])
        expected = {f"{chunk.file_path}:{chunk.start_line}": chunk for chunk in chunks + additions}
        if collection.metadata != {"repo_url": source["repo_url"], "commit_sha": source["commit_sha"]} or set(after["ids"]) != set(expected):
            raise ValueError("Candidate collection does not match the fixed source and chunk experiment")
        for identity, text, meta in zip(after["ids"], after["documents"], after["metadatas"]):
            chunk = expected[identity]
            if text != chunk.content or any(meta.get(key) != getattr(chunk, key) for key in ("file_path", "start_line", "end_line", "chunk_type", "name")):
                raise ValueError("Candidate source text or metadata changed")
        by_id = dict(zip(after["ids"], after["embeddings"]))
        replay_error = max(float(np.max(np.abs(vector-by_id[identity]))) for identity, vector in zip(data["ids"], data["embeddings"]))
        tolerance = float(np.finfo(np.float32).eps)
        if replay_error > tolerance:
            raise ValueError("Existing embeddings changed during a chunk-only experiment")
        variants["baseline"][source["source_id"]] = name
        variants["module_supplement"][source["source_id"]] = candidate
        counts.append({"source_id": source["source_id"], "baseline_chunks": len(chunks), "additional_chunks": len(additions),
                       "candidate_chunks": len(chunks)+len(additions), "reused_vectors": len(chunks),
                       "candidate_collection": candidate, "existing_candidate_reused": reused_candidate,
                       "max_vector_replay_error": replay_error, "vector_replay_tolerance": tolerance,
                       "new_chunks": [asdict(chunk) for chunk in additions]})
    gold = {item.case_id: item for item in bundle.expectations}
    evidence = {item.evidence_id: item for item in bundle.evidence}
    sources = {source.source_id: source for source in bundle.manifest.sources}
    results = {}
    for variant, collections in variants.items():
        turns = []
        for case in bundle.cases:
            source = sources[case.source_id]
            for turn, expected in zip(case.turns, gold[case.case_id].turns):
                start = time.perf_counter()
                hits = vector_store.search(turn.question, source.repo_url, 5, collection_name=collections[case.source_id]) if expected.evidence_required else []
                if any(hit["commit_sha"] != source.commit_sha for hit in hits):
                    raise ValueError("Retrieval crossed the fixed source version")
                turns.append({"case_id": case.case_id, "turn_id": turn.turn_id, "question": turn.question,
                    "evidence_required": expected.evidence_required, "hits": hits,
                    "elapsed_ms": round((time.perf_counter()-start)*1000),
                    "groups": group_coverage(hits, expected.evidence_groups, evidence) if expected.evidence_required else []})
        results[variant] = {"summary": _summary(turns), "turns": turns}
    report = {"format_version": 1, "experiment": "module_supplement_v1", "real_model_requests": 0,
              "answer_quality_evaluated": False, "dataset_fingerprint": bundle.fingerprint,
              "baseline_index_sha256": digest((Path(index_directory)/"index.json").read_bytes()),
              "source_versions": [source.model_dump() for source in bundle.manifest.sources], "top_k": 5,
              "changed_variable": "add_uncovered_python_module_lines", "original_vectors_reused": True,
              "production_default_changed": False, "counts": counts, "variants": results,
              "implementation_sha256": {name: digest((ROOT/name).read_bytes()) for name in
                                        ("indexer/module_chunks.py", "evaluation/retrieval.py", "indexer/chunker.py", "indexer/vector_store.py")}}
    write_json(destination / "comparison.json", report)
    return report
