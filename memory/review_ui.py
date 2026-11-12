"""Human approval stays outside the Agent's tools."""

import json
import streamlit as st

from memory.knowledge import list_cache, review_qa, get_memory_stats, _validate
from security.output import redact_text, safe_error, valid_source_url


def _review(repository, identity, token, approve):
    try:
        review_qa(repository.repo_url, repository.commit_sha, repository.repo_root, identity, token, approve=approve)
        st.session_state["cache_review_notice"] = (True, "已批准使用" if approve else "已撤销批准")
    except (ValueError, OSError, KeyError) as exc:
        st.session_state["cache_review_notice"] = (False, safe_error(exc))


def render_cache_review():
    st.title("历史答案审核")
    repository = st.session_state.get("repository_index")
    if repository is None:
        st.text("请先在问答页选择仓库")
        return
    st.caption(f"{repository.repo_url} · {repository.commit_sha[:12]}")
    if notice := st.session_state.pop("cache_review_notice", None):
        (st.success if notice[0] else st.error)(notice[1])
    try:
        stats = get_memory_stats(repository.repo_url, commit_sha=repository.commit_sha)
        page = st.number_input("页码", min_value=1, value=1, step=1)
        rows = list_cache(repository.repo_url, repository.commit_sha, limit=20, offset=(page-1)*20)
    except (ValueError, OSError) as exc:
        st.error(safe_error(exc))
        return
    st.caption(f"已批准 {stats['count']} · 待审核 {stats['pending_count']} · 共 {stats['total_count']}")
    if not rows:
        st.text("本页没有历史答案")
        return
    by_id = {row["id"]: row for row in rows}
    identity = st.selectbox("历史答案", list(by_id), format_func=lambda key: redact_text(by_id[key]["metadata"].get("question", key))[:80])
    row = by_id[identity]
    status = row["metadata"].get("approval_status", "pending")
    st.text({"pending": "待审核", "approved": "已批准", "revoked": "已撤销"}.get(status, "待审核"))
    st.text(redact_text(row["metadata"].get("question", "")))
    st.text(redact_text(row["metadata"].get("answer", row["document"])))
    valid = False
    try:
        citations = _validate(row["metadata"], row["document"], repository.repo_url, repository.commit_sha, repository.repo_root)
        valid = True
    except (ValueError, KeyError, TypeError, OSError) as exc:
        st.error(safe_error(exc))
    else:
        with st.expander("源码依据"):
            from indexer.citations import SourceCitation
            for value in citations:
                citation = SourceCitation.model_validate(value)
                if valid_source_url(citation.url):
                    st.link_button(citation.label, citation.url)
                st.code(citation.numbered_source, language=None)
    st.button("批准使用", disabled=not valid or status == "approved", on_click=_review,
              args=(repository, identity, row["review_token"], True))
    st.button("撤销批准", disabled=status != "approved", on_click=_review,
              args=(repository, identity, row["review_token"], False))
