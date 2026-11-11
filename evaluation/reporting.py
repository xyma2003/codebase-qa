"""Import declared human ratings, compute reports and retain immutable provenance."""

from datetime import datetime, timezone
import html
from pathlib import Path
import re
from uuid import uuid4
from urllib.parse import quote

from git import Repo
from typing import Literal

from evaluation.archive import write_json, write_rows
from evaluation.dataset import GitSha, Record, Sha256, Text, digest, read_json, read_rows, Review
from evaluation.reviews import load_packet, load_source, review_context
from evaluation.scoring import score_turn, summarize_reviews

ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION = ("evaluation/scoring.py", "evaluation/reviews.py", "evaluation/reporting.py")
REPORT_FILES = ("submission/review.json", "submission/reviews.jsonl", "submission/context.json", "submission/rubric.json",
                "merged-reviews.jsonl", "report.json", "report.md", "badcases.jsonl")


class ReportMetadata(Record):
    format_version: Literal[1]
    report_id: Text
    generated_at: Text
    generator_commit: GitSha
    generator_dirty: bool
    implementation_sha256: dict[str, Sha256]


def _calculate(source, header, rows, metadata):
    root, receipt, bundle, run, base = source
    summary = summarize_reviews(bundle, rows)
    indexed = {(row.case_id, row.turn_id): row for row in rows}
    context = review_context(root, bundle)
    cases, badcases = [], []
    for case, result in zip(context["cases"], summary["cases"]):
        turns = []
        for item in case["turns"]:
            row = indexed[(case["case_id"], item["turn"]["turn_id"])]
            grade = score_turn(bundle.rubric, row)
            tags = list(row.fatal_errors)
            if row.status == "execution_failed":
                tags.append("execution_failure")
            elif row.ratings:
                tags += [name for name, minimum in bundle.rubric.minimum_passing_ratings.items()
                         if getattr(row.ratings, name) < minimum]
            turns.append({**item, "review": row.model_dump(), **grade, "issue_tags": tags})
        record = {**result, "turns": turns}
        cases.append(record)
        if any(turn["passed"] is False for turn in turns):
            badcases.append(record)
    categories = []
    for category in bundle.manifest.category_counts:
        selected = [case for case in cases if case["category"] == category]
        category_rows = [turn["review"] for case in selected for turn in case["turns"]]
        categories.append({"category": category, "required_cases": len(selected), "required_turns": len(category_rows),
                           "reviewed_turns": sum(row["status"] == "reviewed" for row in category_rows),
                           "known_failed_cases": sum(any(turn["passed"] is False for turn in case["turns"]) for case in selected),
                           "finalized": summary["finalized"],
                           "mean_score": sum(case["score"] for case in selected) / len(selected) if summary["finalized"] else None,
                           "pass_rate": sum(case["passed"] for case in selected) / len(selected) if summary["finalized"] else None})
    report = {
        "metadata": metadata.model_dump(), "submission": header.model_dump(),
        "report_kind": "synthetic_review_demo" if header.purpose == "simulation" else "human_review_of_local_fixture",
        "ratings_provenance": "synthetic_fixture" if header.purpose == "simulation" else "human_declared",
        "source_backend": run["backend"], "real_model_quality_evaluated": False,
        "rubric_id": bundle.rubric.rubric_id, "scoring_algorithm": "frozen-rubric-v1",
        "summary": summary, "categories": categories, "cases": cases,
        "known_badcase_count": len(badcases),
    }
    return report, badcases


def _text(value):
    value = html.escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", value)


def render_report(report):
    simulated = report["report_kind"] == "synthetic_review_demo"
    title = "合成评分流程样例" if simulated else "本地模拟输出的人工评审报告"
    origin = "本地构造评分记录" if simulated else "评审者填写的记录"
    summary = report["summary"]
    score = lambda value: "待评审" if value is None else f"{value:.2f}"
    rate = lambda value: "待评审" if value is None else f"{value:.1%}"
    lines = [f"# {title}", "", f"评分来源：{origin}。对象：本地模拟输出。", "",
             f"源运行：{_text(report['submission']['source_run_id'])}", "",
             f"归档 SHA256：{report['submission']['source_archive_sha256']}", "",
             f"评分记录：{summary['reviewed_turns']}/{summary['required_turns']} 轮；题目分母：{summary['required_cases']}。", "",
             f"样例均分：{score(summary['mean_score'])}；样例通过率：{rate(summary['pass_rate'])}。", "",
             "## 分类结果", "", "| 类别 | 题数 | 已填评分轮次 | 已知问题题数 | 样例均分 | 样例通过率 |",
             "| --- | --- | --- | --- | --- | --- |"]
    labels = {"understanding": "项目理解", "localization": "实现定位", "planning": "修改规划", "unanswerable": "无答案"}
    for category in report["categories"]:
        lines.append(f"| {labels[category['category']]} | {category['required_cases']} | {category['reviewed_turns']} | "
                     f"{category['known_failed_cases']} | {score(category['mean_score'])} | {rate(category['pass_rate'])} |")
    lines += ["", "## 已标记的问题", ""]
    for case in report["cases"]:
        failed = [turn for turn in case["turns"] if turn["passed"] is False]
        if not failed:
            continue
        lines += [f"### {_text(case['case_id'])}", ""]
        if len(case["turns"]) > 1:
            lines += ["对话上下文：", ""]
            lines += [f"- {_text(item['turn']['turn_id'])}：{_text(item['turn']['question'])}" for item in case["turns"]]
            lines.append("")
        for turn in failed:
            review = turn["review"]
            lines += [f"**{_text(turn['turn']['turn_id'])}**", "",
                      f"问题：{_text(turn['turn']['question'])}", "",
                      f"输出摘要：{review['output_sha256'] or '未完成输出'}", "",
                      f"标记：{_text('、'.join(turn['issue_tags']))}", "",
                      f"评语或执行原因：{_text(review['rationale'] or review['error'])}", ""]
            if turn["checked_answer"]:
                lines += [f"回答：{_text(turn['checked_answer']['text'])}", ""]
                for citation in turn["checked_answer"]["citations"]:
                    url = (f"{citation['repo_url']}/blob/{citation['commit_sha']}/{quote(citation['file_path'], safe='/')}"
                           f"#L{citation['start_line']}-L{citation['end_line']}")
                    lines += [f"源码：[{_text(citation['file_path'])}:{citation['start_line']}-{citation['end_line']}]({url}) "
                              f"（{citation['commit_sha']}）", ""]
    if report["known_badcase_count"] == 0:
        lines += ["暂无已标记的问题。"]
    return "\n".join(lines).strip() + "\n"


def _inventory(root):
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Report artifacts cannot be symbolic links")
        if path.is_file() and path.relative_to(root).as_posix() != "report-seal.json":
            files[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    if set(files) != set(REPORT_FILES):
        raise ValueError("Report file inventory differs from the fixed format")
    return files


def import_review(archive, repository, packet, output):
    source = load_source(archive, repository)
    header, rows = load_packet(packet, source)
    with Repo(ROOT) as repo:
        metadata = ReportMetadata.model_validate({"format_version": 1, "report_id": str(uuid4()),
            "generated_at": datetime.now(timezone.utc).isoformat(), "generator_commit": repo.head.commit.hexsha,
            "generator_dirty": repo.is_dirty(untracked_files=True),
            "implementation_sha256": {name: digest((ROOT / name).read_bytes()) for name in IMPLEMENTATION}})
    report, badcases = _calculate(source, header, rows, metadata)
    destination = Path(output).resolve()
    if destination.is_relative_to(Path(archive).resolve()) or destination.is_relative_to(Path(packet).resolve()):
        raise ValueError("Report must be outside the source archive and editable review packet")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "submission").mkdir()
    for name in ("review.json", "reviews.jsonl", "context.json", "rubric.json"):
        with (destination / "submission" / name).open("xb") as file:
            file.write((Path(packet) / name).read_bytes())
    write_rows(destination / "merged-reviews.jsonl", [row.model_dump() for row in rows])
    write_json(destination / "report.json", report)
    with (destination / "report.md").open("x", encoding="utf-8") as file:
        file.write(render_report(report))
    write_rows(destination / "badcases.jsonl", badcases)
    write_json(destination / "report-seal.json", {"format_version": 1, "sha256": _inventory(destination)})
    return verify_report(destination, archive, repository)


def verify_report(report_directory, archive, repository):
    root = Path(report_directory)
    if (root / "report-seal.json").is_symlink():
        raise ValueError("Report seal cannot be a symbolic link")
    seal = read_json((root / "report-seal.json").read_bytes())
    if seal != {"format_version": 1, "sha256": _inventory(root)}:
        raise ValueError("Report artifact checksum mismatch")
    source = load_source(archive, repository)
    header, rows = load_packet(root / "submission", source)
    report = read_json((root / "report.json").read_bytes())
    metadata = ReportMetadata.model_validate(report["metadata"])
    if set(metadata.implementation_sha256) != set(IMPLEMENTATION):
        raise ValueError("Report implementation inventory is incomplete")
    expected, badcases = _calculate(source, header, rows, metadata)
    if report != expected:
        raise ValueError("Report scores or provenance differ from source-bound reviews")
    merged = read_rows((root / "merged-reviews.jsonl").read_bytes(), Review)
    if merged != rows:
        raise ValueError("Merged reviews changed execution outcomes or omitted the fixed denominator")
    actual_badcases = [read_json(line) for line in (root / "badcases.jsonl").read_text().splitlines()]
    if actual_badcases != badcases or (root / "report.md").read_text() != render_report(expected):
        raise ValueError("Rendered report or badcase details disagree with scores")
    summary = report["summary"]
    return {"status": "review_report_verified", "report_kind": report["report_kind"],
            "source_archive_sha256": header.source_archive_sha256, "dataset_fingerprint": header.dataset_fingerprint,
            "required_cases": summary["required_cases"], "required_turns": summary["required_turns"],
            "reviewed_turns": summary["reviewed_turns"], "finalized": summary["finalized"],
            "known_badcase_count": len(badcases), "real_model_quality_evaluated": False,
            "report_sha256": digest((root / "report-seal.json").read_bytes())}
