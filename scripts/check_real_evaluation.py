"""Check provider plumbing with a mock API and the real pinned vector index."""

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.archive import pack_archive, verify_archive
from evaluation.dataset import digest, read_json
from evaluation.indexes import build_indexes
from evaluation.reviews import prepare_review
from evaluation.reporting import import_review, verify_report
from indexer.repository_state import get_active
from tests.test_real_evaluation import baseline_fixture, ProviderFixture


def check(directory, package):
    directory.mkdir(parents=True, exist_ok=False)
    active = get_active("https://github.com/xyma2003/codebase-qa")
    dataset = ROOT / "evals/codebase-qa-v1"
    baseline = directory / "synthetic-baseline"
    baseline_fixture(baseline)
    indexes = directory / "indexes"
    index_receipt = build_indexes(dataset, ROOT, indexes)
    provider = ProviderFixture()
    run = directory / "run"
    receipt = asyncio.run(provider.run(dataset, ROOT, run, baseline, indexes))
    assert receipt["backend"] == "provider_fixture" and not receipt["real_model_called"]
    assert receipt["completed_turns"] == 19 and receipt["failed_turns"] == 0
    assert get_active("https://github.com/xyma2003/codebase-qa") == active
    assert all("GOLD_ONLY_TRIPWIRE" not in json.dumps(body) for body in provider.payloads)
    packet, report = directory / "packet", directory / "report"
    prepare_review(run, ROOT, packet)
    report_receipt = import_review(run, ROOT, packet, report)
    assert not report_receipt["finalized"] and not report_receipt["real_model_quality_evaluated"]
    assert report_receipt["report_kind"] == "human_review_of_provider_fixture"
    pack_archive(run, package)
    for command, expected in ((["scripts/verify_evaluation_archive.py", str(run)], receipt),
                              (["scripts/review_evaluation.py", "verify", "--archive", str(run), "--report", str(report)], report_receipt)):
        result = subprocess.run([sys.executable, *command], cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert read_json(result.stdout) == expected
    assert verify_archive(run, ROOT) == receipt and verify_report(report, run, ROOT) == report_receipt
    files = ("agent/qa_agent.py", "agent/tools.py", "evaluation/runner.py", "evaluation/real_runner.py", "evaluation/real_preflight.py",
             "evaluation/provider_model.py", "evaluation/indexes.py", "evaluation/archive.py", "evaluation/reporting.py", "evaluation/review_ui.py",
             "scripts/run_evaluation.py", "scripts/build_evaluation_indexes.py", "scripts/check_real_evaluation.py")
    return {"status": "provider_evaluation_checks_passed", "real_model_requests": 0,
            "mock_sdk_requests": len(provider.payloads), "synthetic_baseline_for_gate_tests": True,
            "real_model_quality_evaluated": False, "app_active_index_unchanged": True,
            "index": index_receipt, "run": receipt, "report": report_receipt, "fresh_process_verification": True,
            "package": {"file": package.name, "bytes": package.stat().st_size, "sha256": digest(package.read_bytes())},
            "implementation_sha256": {name: digest((ROOT / name).read_bytes()) for name in files}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, default=ROOT / ".baseline_runtime/evaluations/20261112-provider-check")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/checks/20261112-provider-evaluation.json")
    parser.add_argument("--package", type=Path, default=ROOT / "docs/checks/20261112-provider-evaluation-local.zip")
    args = parser.parse_args()
    if any(path.exists() for path in (args.work_root, args.output, args.package)):
        raise RuntimeError("Choose new output paths; saved runs are not overwritten")
    result = check(args.work_root.resolve(), args.package.resolve())
    with args.output.open("x", encoding="utf-8") as file:
        file.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("status", "real_model_requests", "mock_sdk_requests", "app_active_index_unchanged")}))
