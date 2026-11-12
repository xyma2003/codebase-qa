"""Compare only retrieval strategy, reusing the same successful vector collection."""

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.archive import write_json
from evaluation.dataset import digest, validate_bundle
from evaluation.indexes import check_indexes
from evaluation.retrieval import group_coverage, _summary
from indexer import vector_store
from indexer.hybrid import hybrid_search, RRF_CONSTANT, MAX_QUERY_TOKENS, LEXICAL_LIMIT


def compare(dataset, repository, indexes, output):
    bundle = validate_bundle(dataset, repository)
    manifest = check_indexes(indexes, bundle, repository)
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=False)
    sources = {source["source_id"]: source for source in manifest["sources"]}
    gold = {case.case_id: case for case in bundle.expectations}
    evidence = {item.evidence_id: item for item in bundle.evidence}
    variants = {}
    for strategy in ("semantic", "hybrid"):
        turns = []
        for case in bundle.cases:
            source = sources[case.source_id]
            for turn, expected in zip(case.turns, gold[case.case_id].turns):
                started = time.perf_counter()
                hits = []
                if expected.evidence_required:
                    hits = (hybrid_search(source["repo_root"], source["repo_url"], source["commit_sha"], turn.question, source["collection_name"])
                            if strategy == "hybrid" else vector_store.search(turn.question, source["repo_url"], 5, collection_name=source["collection_name"]))
                turns.append({"case_id": case.case_id, "turn_id": turn.turn_id, "question": turn.question,
                              "evidence_required": expected.evidence_required, "hits": hits,
                              "elapsed_ms": round((time.perf_counter()-started)*1000),
                              "groups": group_coverage(hits, expected.evidence_groups, evidence) if expected.evidence_required else []})
        variants[strategy] = {"summary": _summary(turns), "turns": turns}
    result = {"format_version": 1, "experiment": "hybrid_retrieval_v1", "dataset_fingerprint": bundle.fingerprint,
              "source_versions": [source.model_dump() for source in bundle.manifest.sources],
              "baseline_index_sha256": digest((Path(indexes)/"index.json").read_bytes()), "variants": variants,
              "changed_variable": "semantic_vs_fixed_lexical_rrf", "top_k": 5, "rrf_constant": RRF_CONSTANT,
              "max_query_tokens": MAX_QUERY_TOKENS, "lexical_limit": LEXICAL_LIMIT,
              "new_embeddings": 0, "real_model_requests": 0, "production_default_changed": False,
              "answer_quality_evaluated": False,
              "implementation_sha256": {name: digest((ROOT/name).read_bytes()) for name in
                                        ("indexer/hybrid.py", "agent/tools.py", "agent/qa_agent.py", "scripts/compare_hybrid_retrieval.py")}}
    write_json(destination / "comparison.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT/"evals/codebase-qa-v1")
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--indexes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.dataset, args.repository, args.indexes, args.output)
    print(json.dumps({"status": "hybrid_retrieval_comparison_completed", "real_model_requests": 0,
                      "summaries": {name: variant["summary"] for name, variant in result["variants"].items()}}))
