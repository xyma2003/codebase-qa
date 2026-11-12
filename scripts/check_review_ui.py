"""Exercise the review page using a saved archive and explicit synthetic scores."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest
from evaluation.archive import verify_archive
from evaluation.dataset import digest, read_json
from evaluation.reporting import verify_report
from evaluation.review_workspace import new_workspace, open_workspace, report_package


def check(directory, package):
    original = ROOT / "docs/checks/20261111-evaluation-runner-local.zip"
    receipt = read_json((ROOT / "docs/checks/20261111-evaluation-runner.json").read_bytes())
    if digest(original.read_bytes()) != receipt["portable_package"]["sha256"]:
        raise ValueError("Saved source archive package changed")
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "source"
    with zipfile.ZipFile(original) as zipped:
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in zipped.namelist()):
            raise ValueError("Unsafe archive path")
        zipped.extractall(source)
    before = verify_archive(source, ROOT)
    workspace = new_workspace(source, ROOT, directory, purpose="simulation")
    script = ("from evaluation.review_ui import render_review_page\n"
              f"render_review_page(repository={str(ROOT)!r}, directory={str(directory)!r})")
    app = AppTest.from_string(script).run(timeout=30)

    def item(kind, label):
        return next(element for element in getattr(app, kind) if element.label == label)

    def click(label):
        item("button", label).click().run(timeout=30)
        if app.exception:
            raise AssertionError([element.value for element in app.exception])

    item("text_input", "运行归档目录").set_value(str(source))
    item("selectbox", "评审记录").select(str(workspace.packet))
    click("打开评审")
    item("selectbox", "题目").select("U04").run(timeout=30)
    item("selectbox", "轮次").select("T2").run(timeout=30)
    assert item("selectbox", "正确性（40%）").value is None
    click("保存本轮")
    assert any("四项分数" in error.value for error in app.error)
    for label, value in (("正确性（40%）", 0), ("证据（30%）", 0), ("完整性（20%）", 0), ("边界说明（10%）", 2)):
        item("selectbox", label).select(value)
    item("text_input", "评审者").set_value("synthetic-ui-check")
    item("text_area", "评分理由").set_value("合成界面操作记录：验证保存、多轮上下文和报告，不是对该回答的真实语义评分。")
    click("保存本轮")
    reopened = open_workspace(source, ROOT, workspace.packet)
    assert sum(row.status == "reviewed" for row in reopened.rows) == 1
    click("生成报告")
    report = Path(app.session_state["review_report"])
    result = verify_report(report, source, ROOT)
    data = read_json((report / "report.json").read_bytes())
    assert result["reviewed_turns"] == 1 and result["required_turns"] == 19
    assert not result["finalized"] and data["summary"]["mean_score"] is None
    assert data["summary"]["pass_rate"] is None and data["known_badcase_count"] == 1
    assert result["report_kind"] == "synthetic_review_demo" and not result["real_model_quality_evaluated"]
    assert len(app.get("download_button")) == 2
    with package.open("xb") as file:
        file.write(report_package(report, source, ROOT))
    unpacked = directory / "unpacked-report"
    with zipfile.ZipFile(package) as zipped:
        zipped.extractall(unpacked)
    fresh = subprocess.run([sys.executable, str(ROOT / "scripts/review_evaluation.py"), "verify",
                            "--archive", str(source), "--report", str(unpacked)],
                           cwd=ROOT, text=True, capture_output=True, timeout=60)
    if fresh.returncode:
        raise RuntimeError(fresh.stderr)
    assert read_json(fresh.stdout) == result
    assert verify_archive(source, ROOT) == before
    return {"status": "review_ui_checks_passed", "source_archive_sha256": before["archive_sha256"],
            "source_archive_unchanged": True, "new_model_requests": 0, "real_model_quality_evaluated": False,
            "ratings_provenance": "synthetic_fixture", "reviewed_case": "U04", "reviewed_turn": "T2",
            "report": result, "fresh_process_verification": True,
            "package": {"file": package.name, "sha256": digest(package.read_bytes()), "bytes": package.stat().st_size},
            "implementation_sha256": {name: digest((ROOT / name).read_bytes()) for name in
                ("app.py", "evaluation/review_workspace.py", "evaluation/review_ui.py", "pages/1_评测评审.py", "scripts/check_review_ui.py")},
            "local_paths": {"archive": str(source), "packet": str(workspace.packet), "report": str(report)}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, default=ROOT / ".baseline_runtime/reviews/20261112-ui-check")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/checks/20261112-review-ui.json")
    parser.add_argument("--package", type=Path, default=ROOT / "docs/checks/20261112-review-ui-local.zip")
    args = parser.parse_args()
    if any(path.exists() for path in (args.work_root, args.output, args.package)):
        raise RuntimeError("Choose new output paths; saved records are not overwritten")
    result = check(args.work_root, args.package)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        file.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("status", "new_model_requests", "real_model_quality_evaluated")}))
