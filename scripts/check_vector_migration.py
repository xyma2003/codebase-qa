"""Fixed queries: archive old Chroma results, then verify SQLite independently."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def queries():
    source = ROOT / "evals/codebase-qa-v1/cases.jsonl"
    records = [json.loads(line) for line in source.read_text().splitlines()]
    return [turn["question"] for row in records for turn in row["turns"]], hashlib.sha256(source.read_bytes()).hexdigest()


def baseline(source, output):
    from scripts.export_legacy_vectors import legacy_client
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    texts, checksum = queries()
    embed = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    vectors = embed(input=texts)
    client = legacy_client(source)
    rows = []
    for item in sorted(client.list_collections(), key=lambda x: x.name):
        collection = client.get_collection(item.name, embedding_function=None)
        started = time.perf_counter()
        result = collection.query(query_embeddings=vectors, n_results=5)
        rows.append({"name": item.name, "metric": collection.configuration_json["hnsw"]["space"],
            "ids": result["ids"], "distances": result["distances"], "seconds": time.perf_counter()-started})
    data = {"schema": 1, "query_file_sha256": checksum, "questions": texts,
        "vectors": [v.tolist() for v in vectors], "collections": rows, "real_model_requests": 0}
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(data, file, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"collections": len(rows), "queries": len(texts), "sha256": hashlib.sha256(Path(output).read_bytes()).hexdigest()}))


def check(source, reference, output):
    from indexer.embeddings import SentenceTransformerEmbeddingFunction
    from security.vector_client import local_vector_client
    texts, checksum = queries()
    expected = json.loads(Path(reference).read_bytes())
    if expected["schema"] != 1 or expected["questions"] != texts or expected["query_file_sha256"] != checksum:
        raise ValueError("Query baseline differs")
    vectors = SentenceTransformerEmbeddingFunction()(input=texts)
    error = float(np.max(np.abs(np.asarray(vectors)-np.asarray(expected["vectors"], dtype=np.float32))))
    if error != 0:
        raise ValueError("Embedding values differ; do not change model parameters to retry")
    client = local_vector_client(source)
    records = []
    for row in expected["collections"]:
        c = client.get_collection(row["name"], embedding_function=None)
        if c.metric != row["metric"]:
            raise ValueError("Distance metric differs")
        started = time.perf_counter()
        actual = c.query(query_embeddings=vectors, n_results=5)
        duration = time.perf_counter()-started
        for question, ids, distances, before_ids, before_distances in zip(texts, actual["ids"], actual["distances"], row["ids"], row["distances"]):
            records.append({"collection": row["name"], "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
                "ordered_ids_equal": ids == before_ids, "set_equal": set(ids) == set(before_ids),
                "distance_max_error": max((abs(a-b) for a,b in zip(distances,before_distances)), default=0),
                "actual_ids": ids, "legacy_ids": before_ids, "actual_distances": distances, "legacy_distances": before_distances})
        print(json.dumps({"collection": c.name, "queries": len(texts), "sqlite_seconds": duration, "legacy_seconds": row["seconds"]}))
    report = {"schema": 1, "queries": len(texts), "collection_queries": len(records), "embedding_max_error": error,
        "ordered_top5_equal": sum(x["ordered_ids_equal"] for x in records), "top5_sets_equal": sum(x["set_equal"] for x in records),
        "distance_max_error": max(x["distance_max_error"] for x in records), "real_model_requests": 0,
        "reference_sha256": hashlib.sha256(Path(reference).read_bytes()).hexdigest(), "records": records}
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    # Exact search can legitimately improve on an approximate index, but such a
    # difference must be reviewed; never hide it behind a permissive threshold.
    if report["ordered_top5_equal"] != len(records) or report["distance_max_error"] > 1e-5:
        raise ValueError("Retrieval differs; inspect the archived comparison before acceptance")
    print(json.dumps({k:v for k,v in report.items() if k!='records'}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.legacy:
        baseline(args.source, args.output)
    else:
        if not args.reference:
            parser.error("--reference required")
        check(args.source, args.reference, args.output)
