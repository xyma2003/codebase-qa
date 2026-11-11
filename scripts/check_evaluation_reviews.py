"""Exercise review import using the existing local archive, with synthetic ratings."""

import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.archive import verify_archive
from evaluation.dataset import digest, read_json
from evaluation.reviews import prepare_review
from evaluation.reporting import IMPLEMENTATION, import_review, verify_report


def check(work_root, package, sample_report):
    source_zip = ROOT / "docs/checks/20261111-evaluation-runner-local.zip"
    source_receipt = read_json((ROOT / "docs/checks/20261111-evaluation-runner.json").read_bytes())
    if digest(source_zip.read_bytes()) != source_receipt["portable_package"]["sha256"]:
        raise ValueError("Original local archive package changed")
    work_root.mkdir(parents=True, exist_ok=False)
    source = work_root / "archive"
    with zipfile.ZipFile(source_zip) as archive:
        if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts for name in archive.namelist()):
            raise ValueError("Unsafe packaged archive path")
        archive.extractall(source)
    before = verify_archive(source, ROOT)
    packet = work_root / "packet"
    prepare_review(source, ROOT, packet, purpose="simulation")
    rows = [read_json(line) for line in (packet / "reviews.jsonl").read_text().splitlines()]
    synthetic = [dict(row, status="reviewed", ratings=dict(correctness=0, evidence=0, completeness=0, boundaries=2),
                      reviewer="local-review-fixture",
                      rationale="合成评分记录：只验证导入、分母与报表流程，不是人工逐题判读，不用于真实模型成绩。") for row in rows]
    editable = packet / "reviews.jsonl"
    editable.write_text(json.dumps(synthetic[0], ensure_ascii=False) + "\n")
    partial = import_review(source, ROOT, packet, work_root / "partial-report")
    partial_data = read_json((work_root / "partial-report/report.json").read_bytes())
    assert partial["reviewed_turns"] == 1 and not partial["finalized"]
    assert partial_data["summary"]["mean_score"] is None and partial_data["summary"]["pass_rate"] is None
    assert all(item["mean_score"] is None for item in partial_data["categories"])
    editable.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in synthetic) + "\n")
    report_dir = work_root / "full-report"
    full = import_review(source, ROOT, packet, report_dir)
    assert full["reviewed_turns"] == 19 and full["finalized"] and full["known_badcase_count"] == 16
    assert full["report_kind"] == "synthetic_review_demo" and not full["real_model_quality_evaluated"]
    # Later edits to the working packet cannot rewrite the earlier import.
    assert verify_report(work_root / "partial-report", source, ROOT) == partial
    result = subprocess.run([sys.executable, str(ROOT / "scripts/review_evaluation.py"), "verify",
                             "--archive", str(source), "--report", str(report_dir)],
                            capture_output=True, text=True, timeout=60, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(result.stderr)
    assert read_json(result.stdout) == full
    with zipfile.ZipFile(package, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(report_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(report_dir).as_posix())
    with sample_report.open("x", encoding="utf-8") as file:
        file.write((report_dir / "report.md").read_text())
    assert verify_archive(source, ROOT) == before
    return {"status": "review_import_checks_passed", "source_archive_unchanged": True, "new_model_requests": 0,
            "real_model_quality_evaluated": False, "ratings_provenance": "synthetic_fixture",
            "source_archive_sha256": before["archive_sha256"], "partial_report": partial, "full_demo_report": full,
            "fresh_process_verification": True,
            "report_package": {"file": package.name, "sha256": digest(package.read_bytes()), "bytes": package.stat().st_size},
            "sample_markdown": {"file": sample_report.name, "sha256": digest(sample_report.read_bytes())},
            "implementation_sha256": {name: digest((ROOT / name).read_bytes()) for name in IMPLEMENTATION},
            "scripts_sha256": {name: digest((ROOT / name).read_bytes()) for name in
                               ("scripts/review_evaluation.py", "scripts/check_evaluation_reviews.py")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, default=ROOT / ".baseline_runtime/reviews/20261111-import-check")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/checks/20261111-review-import.json")
    parser.add_argument("--package", type=Path, default=ROOT / "docs/checks/20261111-review-import-local.zip")
    parser.add_argument("--sample-report", type=Path, default=ROOT / "docs/checks/20261111-review-import-sample.md")
    args = parser.parse_args()
    if any(path.exists() for path in (args.work_root, args.output, args.package, args.sample_report)):
        raise RuntimeError("Choose new output paths; saved records are not overwritten.")
    report = check(args.work_root, args.package, args.sample_report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "demo_rated_turns": 19, "new_model_requests": 0,
                      "real_model_quality_evaluated": False}))
