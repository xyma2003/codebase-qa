"""Small Streamlit view over the existing review packet and scoring services."""

from pathlib import Path
from urllib.parse import quote

import streamlit as st

from evaluation.dataset import read_json
from evaluation.review_workspace import create_report, new_workspace, open_workspace, report_package, save_grade
from evaluation.scoring import summarize_reviews

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / ".baseline_runtime/reviews/ui"
CATEGORIES = {"understanding": "项目理解", "localization": "实现定位", "planning": "修改规划", "unanswerable": "无答案"}
DIMENSIONS = {"correctness": "正确性", "evidence": "证据", "completeness": "完整性", "boundaries": "边界说明"}
STATES = {"pending_review": "待评审", "reviewed": "已评审", "execution_failed": "执行失败", "not_run": "未运行"}


def _excerpt(source, evidence, label):
    path = evidence["file_path"]
    start, end = evidence["start_line"], evidence["end_line"]
    url = f"{source['repo_url']}/blob/{source['commit_sha']}/{quote(path, safe='/')}#L{start}-L{end}"
    st.link_button(f"{label} · {path}:{start}-{end}", url)
    st.caption(f"Commit {source['commit_sha']}")
    st.code("\n".join(f"{number} | {line}" for number, line in
                      enumerate(evidence["source_text"].splitlines(), start)), language=None)


def _answer(turn):
    st.text(turn["turn"]["question"])
    answer = turn["checked_answer"]
    if answer:
        # Archive/model text is material, never executable HTML or image markup.
        st.text(answer["text"])
        if answer["citations"] or answer["issues"]:
            with st.expander("回答引用与检查结果"):
                for citation in answer["citations"]:
                    _excerpt(citation, citation, citation["reference_id"])
                for issue in answer["issues"]:
                    st.text(f"{issue['reference_id'] or ''} {issue['message']}".strip())
    else:
        st.text({"failed": "执行失败", "blocked": "阻塞", "not_run": "未运行"}.get(
            turn["outcome"]["status"], turn["outcome"]["status"]))
        if turn["error"]:
            st.code(turn["error"].get("message", str(turn["error"])), language=None)


def _references(context, expected):
    evidence = {item["evidence_id"]: item for item in context["evidence"]}
    sources = {item["source_id"]: item for item in context["sources"]}
    if not expected["evidence_required"]:
        st.text("本轮不强制要求源码引用")
    for group in expected["evidence_groups"]:
        with st.expander(f"参考依据 · {group['group_id']}（满足任一项）"):
            for reference in group["any_of"]:
                item = evidence[reference]
                _excerpt(sources[item["source_id"]], item, reference)


def _grade_form(workspace, row, prefix):
    rubric = workspace.bundle.rubric
    with st.expander("评分规则"):
        for dimension in rubric.dimensions:
            st.text(f"{DIMENSIONS[dimension.dimension]} · 权重 {dimension.weight}% · "
                    f"通过要求 ≥ {rubric.minimum_passing_ratings[dimension.dimension]}")
            for value, text in dimension.anchors.items():
                st.text(f"{value}：{text}")
        for code, text in rubric.fatal_errors.items():
            st.text(f"{code}：{text}")
        for text in rubric.notes:
            st.text(text)
    with st.form(f"{prefix}:form"):
        columns = st.columns(4)
        ratings = {}
        for column, dimension in zip(columns, rubric.dimensions):
            name = dimension.dimension
            with column:
                ratings[name] = st.selectbox(f"{DIMENSIONS[name]}（{dimension.weight}%）", [0, 1, 2],
                    index=getattr(row.ratings, name) if row.ratings else None,
                    placeholder="选择分数", key=f"{prefix}:{name}")
        fatal = st.multiselect("致命错误", list(rubric.fatal_errors), default=row.fatal_errors,
                               format_func=lambda code: f"{code} · {rubric.fatal_errors[code]}", key=f"{prefix}:fatal")
        reviewer = st.text_input("评审者", value=row.reviewer or st.session_state.get("review_last_reviewer", ""), key=f"{prefix}:reviewer")
        rationale = st.text_area("评分理由", value=row.rationale, key=f"{prefix}:rationale")
        save = st.form_submit_button("保存本轮", type="primary")
        clear = st.form_submit_button("清除本轮评分", disabled=row.status != "reviewed")
    if save or clear:
        if save and (any(value is None for value in ratings.values()) or not reviewer.strip() or not rationale.strip()):
            st.error("请填写四项分数、评审者和评分理由")
            return
        try:
            updated = save_grade(workspace, row.case_id, row.turn_id, ratings=ratings, fatal_errors=fatal,
                                 reviewer=reviewer, rationale=rationale, clear=clear)
        except (ValueError, OSError) as exc:
            st.error(f"保存失败：{exc}")
            return
        if save:
            st.session_state["review_last_reviewer"] = reviewer.strip()
        st.session_state["review_active"] = (str(updated.archive), str(updated.packet), updated.revision)
        st.session_state.pop("review_report", None)
        st.session_state["review_notice"] = "本轮评分已清除" if clear else "本轮评分已保存"
        st.rerun()


def render_review_page(*, repository=ROOT, directory=WORKSPACE):
    st.set_page_config(page_title="评测评审 · Codebase QA", layout="wide")
    st.title("评测评审")
    directory = Path(directory).resolve()
    packets = sorted(path.parent for path in (directory / "packets").glob("*/review.json"))
    active = st.session_state.get("review_active")
    options = [None, *map(str, packets)]
    selection = active[1] if active and active[1] in options else None
    with st.sidebar:
        with st.form("open_review"):
            archive = st.text_input("运行归档目录", value=active[0] if active else "", key="review_archive_input")
            packet = st.selectbox("评审记录", options, index=options.index(selection),
                format_func=lambda path: Path(path).name[:12] if path else "新建评审", key="review_packet_choice")
            opened = st.form_submit_button("打开评审", type="primary")
        if opened:
            try:
                if not archive.strip():
                    raise ValueError("请填写运行归档目录")
                workspace = (open_workspace(archive.strip(), repository, packet) if packet else
                             new_workspace(archive.strip(), repository, directory))
                st.session_state["review_active"] = (str(workspace.archive), str(workspace.packet), workspace.revision)
                st.session_state.pop("review_report", None)
            except (ValueError, OSError) as exc:
                st.session_state.pop("review_active", None)
                st.session_state.pop("review_report", None)
                st.error(f"打开失败：{exc}")
        active = st.session_state.get("review_active")
        if active:
            st.caption("评审包")
            st.code(active[1], language=None)
    if not active:
        return
    try:
        workspace = open_workspace(active[0], repository, active[1])
    except (ValueError, OSError) as exc:
        st.error(f"评审包校验失败：{exc}")
        return
    if workspace.revision != active[2]:
        st.error("评审记录已被更新，请重新打开后再继续")
        return
    if notice := st.session_state.pop("review_notice", None):
        st.success(notice)
    summary = summarize_reviews(workspace.bundle, workspace.rows)
    origin = "合成评分记录" if workspace.header.purpose == "simulation" else "人工填写评分"
    st.caption(f"本地模拟输出 · {origin} · 已评审 {summary['reviewed_turns']}/{summary['required_turns']} 轮 · {summary['required_cases']} 题")
    with st.expander("归档信息"):
        st.text(f"运行：{workspace.header.source_run_id}")
        st.text(f"归档 SHA256：{workspace.header.source_archive_sha256}")
        st.text(f"数据集：{workspace.header.dataset_fingerprint}")
        st.text(f"评审记录：{workspace.header.review_id}")
        st.text(f"归档目录：{workspace.archive}")
    cases = {case["case_id"]: case for case in workspace.context["cases"]}
    scope = workspace.header.review_id
    case_id = st.selectbox("题目", list(cases), key=f"review_case:{scope}",
        format_func=lambda key: f"{key} · {CATEGORIES[cases[key]['category']]} · {cases[key]['turns'][0]['turn']['question']}")
    case = cases[case_id]
    turns = {item["turn"]["turn_id"]: item for item in case["turns"]}
    turn_id = st.selectbox("轮次", list(turns), key=f"review_turn:{scope}:{case_id}")
    turn = turns[turn_id]
    row = next(row for row in workspace.rows if (row.case_id, row.turn_id) == (case_id, turn_id))
    st.caption(STATES[row.status] + (" · 阻塞" if turn["outcome"]["status"] == "blocked" else ""))
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("问题与回答")
        earlier = case["turns"][:list(turns).index(turn_id)]
        if earlier:
            with st.expander("此前对话", expanded=True):
                for previous in earlier:
                    _answer(previous)
        _answer(turn)
    with right:
        st.subheader("核对要点与源码")
        expected = turn["expected"]
        for item in expected["checkpoints"]:
            st.text(f"• {item}")
        _references(workspace.context, expected)
        with st.expander("不应出现的结论与评审说明"):
            for item in expected["forbidden_claims"]:
                st.text(f"• {item}")
            if expected["expected_touchpoints"]:
                st.text("相关文件：" + "、".join(expected["expected_touchpoints"]))
            st.text(expected["review_note"])
    if row.status in {"pending_review", "reviewed"}:
        _grade_form(workspace, row, f"grade:{scope}:{case_id}:{turn_id}:{workspace.revision}")
    else:
        st.text("本轮没有完成输出，保留原执行状态")
    st.divider()
    if st.button("生成报告"):
        try:
            report, _ = create_report(workspace, directory)
            st.session_state["review_report"] = str(report)
        except (ValueError, OSError) as exc:
            st.error(f"报告生成失败：{exc}")
    if report_path := st.session_state.get("review_report"):
        try:
            report = Path(report_path)
            package = report_package(report, workspace.archive, repository)
            data = read_json((report / "report.json").read_bytes())
            result = data["summary"]
            st.subheader("合成评分流程样例" if workspace.header.purpose == "simulation" else "本地模拟输出的人工评审报告")
            st.text(f"已评审 {result['reviewed_turns']}/{result['required_turns']} 轮 · 已标记问题 {data['known_badcase_count']} 题")
            st.text("样例均分：待评审 · 样例通过率：待评审" if not result["finalized"] else
                    f"样例均分：{result['mean_score']:.2f} · 样例通过率：{result['pass_rate']:.1%}")
            st.dataframe([{"类别": CATEGORIES[item["category"]], "题数": item["required_cases"],
                           "已评审轮次": item["reviewed_turns"], "已标记问题": item["known_failed_cases"]}
                          for item in data["categories"]], hide_index=True)
            st.download_button("下载报告（Markdown）", (report / "report.md").read_bytes(), "review-report.md", mime="text/markdown")
            st.download_button("下载报告包（ZIP）", package, "review-report.zip", mime="application/zip")
            st.code(str(report), language=None)
        except (ValueError, OSError) as exc:
            st.error(f"报告校验失败：{exc}")
