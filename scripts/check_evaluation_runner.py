"""Save local success/failure receipts and a portable, independently checked archive."""

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.archive import pack_archive
from evaluation.dataset import digest, validate_bundle
from evaluation.local_model import LocalModel
from evaluation.runner import RUNTIME_FILES, run_local


async def check(run_root, package):
    dataset = ROOT / "evals/codebase-qa-v1"
    bundle = validate_bundle(dataset, ROOT)
    success = await run_local(dataset, ROOT, run_root / "success")
    assert success["completed_turns"] == 19 and success["failed_turns"] == 0
    question = next(case.turns[0].question for case in bundle.cases if case.case_id == "L04")
    failure = await run_local(dataset, ROOT, run_root / "failure", backend=LocalModel(fail_question=question))
    assert (failure["completed_turns"], failure["failed_turns"], failure["blocked_turns"], failure["not_run_turns"]) == (8, 1, 1, 9)
    pack_archive(run_root / "success", package)
    with tempfile.TemporaryDirectory(prefix="codebase-qa-archive-readback-") as directory:
        with zipfile.ZipFile(package) as archive:
            assert not any(name.startswith("_state/") for name in archive.namelist())
            archive.extractall(directory)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_evaluation_archive.py"), directory],
                                capture_output=True, text=True, timeout=60, cwd=ROOT)
        if result.returncode:
            raise RuntimeError(result.stderr)
        assert json.loads(result.stdout) == success
    metrics = [json.loads(path.read_text()) for path in (run_root / "success/turns").glob("*/metrics.json")]
    requests = sum(row["model_requests"] for row in metrics)
    assert requests == 57 and all(row["cost"] is None and row["provider_usage"] is None for row in metrics)
    return {
        "status": "local_runner_checks_passed", "real_model_called": False, "quality_evaluated": False,
        "success_run": success, "injected_failure_run": failure,
        "success_model_requests": requests, "independent_process_zip_readback": True,
        "portable_package": {"file": package.name, "sha256": digest(package.read_bytes()), "bytes": package.stat().st_size},
        "runtime_file_sha256": {name: digest((ROOT / name).read_bytes()) for name in RUNTIME_FILES},
        "checker_sha256": {name: digest((ROOT / name).read_bytes()) for name in
                          ("scripts/run_evaluation.py", "scripts/verify_evaluation_archive.py", "scripts/check_evaluation_runner.py")},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, default=ROOT / ".baseline_runtime/evaluations/20261111-runner-check")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/checks/20261111-evaluation-runner.json")
    parser.add_argument("--package", type=Path, default=ROOT / "docs/checks/20261111-evaluation-runner-local.zip")
    args = parser.parse_args()
    if args.output.exists() or args.package.exists() or args.run_root.exists():
        raise RuntimeError("Choose new output, package and run-root paths; existing evidence is not overwritten.")
    report = asyncio.run(check(args.run_root, args.package))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "completed_local_turns": 19,
                      "model_requests": 57, "quality_evaluated": False, "real_model_called": False}))
