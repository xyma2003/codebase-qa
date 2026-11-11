"""Check frozen development cases and rubric without running an inference batch."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.dataset import validate_bundle
from evaluation.scoring import summarize_reviews


def check(dataset, repository):
    bundle = validate_bundle(dataset, repository)
    summary = summarize_reviews(bundle, bundle.reviews)
    assert not summary["finalized"] and summary["mean_score"] is None and summary["pass_rate"] is None
    inputs = bundle.model_inputs()
    assert all(set(row) == {"case_id", "source", "turns"} for row in inputs)
    sources = {source.source_id: source for source in bundle.manifest.sources}
    report = {
        "status": "dataset_checks_passed", "evaluation_status": "not_run", "chat_model_called": False,
        "dataset_id": bundle.manifest.dataset_id, "dataset_fingerprint": bundle.fingerprint,
        "case_count": len(bundle.cases), "turn_count": bundle.manifest.turn_count,
        "category_counts": dict(Counter(case.category for case in bundle.cases)),
        "multi_turn_cases": [case.case_id for case in bundle.cases if len(case.turns) > 1],
        "evidence_count": len(bundle.evidence),
        "evidence_source_file_count": len({(item.source_id, item.file_path) for item in bundle.evidence}),
        "sources": [source.model_dump() for source in bundle.manifest.sources],
        "evidence_checks": [{"evidence_id": item.evidence_id, "file_path": item.file_path,
                             "start_line": item.start_line, "end_line": item.end_line, "file_sha256": item.file_sha256,
                             "url": f"{sources[item.source_id].repo_url}/blob/{sources[item.source_id].commit_sha}/"
                                    f"{quote(item.file_path, safe='/')}#L{item.start_line}-L{item.end_line}"}
                            for item in bundle.evidence],
        "review_template_statuses": dict(Counter(row.status for row in bundle.reviews)),
        "rubric_weights": {item.dimension: item.weight for item in bundle.rubric.dimensions},
        "review_summary": summary, "model_inputs_exclude_gold_and_categories": True,
        "payload_sha256": bundle.manifest.payload_sha256,
    }
    paths = ("evaluation/dataset.py", "evaluation/scoring.py", "scripts/check_evaluation.py")
    report["checker_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/codebase-qa-v1")
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/checks/20261111-evaluation.json")
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.exists():
        raise RuntimeError("Choose a new --output; saved evidence is not overwritten.")
    report = check(args.dataset, args.repository)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "case_count", "turn_count", "evidence_count", "evaluation_status")}))
