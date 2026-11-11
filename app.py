import asyncio
import re
import streamlit as st
import nest_asyncio

# uvloop (installed as a dep by chromadb/pydantic-ai) is incompatible with
# nest_asyncio — reset to the standard asyncio policy before patching.
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
nest_asyncio.apply()

from indexer.repository_service import (
    get_current_repository, get_repository_version, import_repository,
    list_repository_versions,
)
from indexer.snapshots import normalize_repo_url
from agent.qa_agent import ask_stream
from memory.knowledge import get_memory_stats
from agent.evidence import CheckedAnswer
from agent.conversations import create_conversation
from indexer.overview import build_repository_overview

st.set_page_config(page_title="Codebase QA", page_icon="🔍", layout="wide")
st.title("🔍 Codebase Q&A")
st.caption("输入 GitHub 仓库地址，用自然语言提问代码相关问题")


def show_sources(result):
    if result is None:
        return
    with st.expander("源码引用"):
        for citation in result.citations:
            label = re.sub(r"([\\\[\]])", r"\\\1", citation.label).replace("\n", " ").replace("\r", " ")
            st.markdown(f"[{citation.reference_id} · {label}]({citation.url})")
            st.caption(f"Commit {citation.commit_sha}")
            st.code(citation.numbered_source, language=None)
        for issue in result.issues:
            st.write(f"{issue.reference_id}：{issue.message}" if issue.reference_id else issue.message)


def clear_repository():
    for key in ("repository_index", "repo_url", "repo_root", "repo_overview", "conversation_id"):
        st.session_state.pop(key, None)
    st.session_state["messages"] = []


def bind_repository(repository):
    previous = st.session_state.get("repository_index")
    if previous is None or previous.collection_name != repository.collection_name:
        clear_repository()
    st.session_state["repository_index"] = repository
    st.session_state["repo_url"] = repository.repo_url
    st.session_state["repo_root"] = repository.repo_root


def import_selected_repository(repo_url, refresh=False):
    progress = st.progress(0, text="正在准备代码版本...")
    try:
        def update_progress(done, total):
            progress.progress(done / total, text=f"已索引 {done}/{total} 个代码块")
        repository = import_repository(repo_url, refresh=refresh, progress_callback=update_progress)
        bind_repository(repository)
        st.session_state["version_selector_epoch"] = st.session_state.get("version_selector_epoch", 0) + 1
        st.rerun()
    except Exception as exc:
        st.error(f"导入失败：{exc}")
    finally:
        progress.empty()

# --- 仓库索引区 ---
with st.sidebar:
    st.subheader("📦 仓库索引")
    repo_url = st.text_input(
        "GitHub 仓库地址",
        placeholder="https://github.com/owner/repo",
        key="repo_input",
    )

    if repo_url:
        try:
            repo_url = normalize_repo_url(repo_url)
            if st.session_state.get("repo_url") != repo_url:
                clear_repository()
            current = get_current_repository(repo_url)
            if current is None:
                if st.button("克隆并索引", type="primary"):
                    import_selected_repository(repo_url)
            else:
                st.success("已索引 ✓")
                versions = list_repository_versions(repo_url)
                selected = st.session_state.get("repository_index", current)
                options = [version.commit_sha for version in versions]
                initial = options.index(selected.commit_sha) if selected.commit_sha in options else options.index(current.commit_sha)
                revision = st.selectbox(
                    "代码版本", options, index=initial, format_func=lambda sha: sha[:12],
                    key=f"version_{repo_url}_{current.collection_name}_{st.session_state.get('version_selector_epoch', 0)}",
                )
                bind_repository(get_repository_version(repo_url, revision))
                if st.button("更新索引"):
                    import_selected_repository(repo_url, refresh=True)
                if st.button("新建对话"):
                    st.session_state.pop("conversation_id", None)
                    st.session_state["messages"] = []
                    st.rerun()
        except (ValueError, OSError) as exc:
            clear_repository()
            st.error(str(exc))
    else:
        clear_repository()

    st.divider()
    st.caption("支持的文件类型：.py .js .ts .go .java .rs .cpp .rb .swift 等")

    # 知识库统计
    if "repo_url" in st.session_state:
        stats = get_memory_stats(
            st.session_state["repo_url"],
            commit_sha=st.session_state["repository_index"].commit_sha,
        )
        if stats["count"] > 0:
            st.success(f"💡 知识库：{stats['count']} 条")
        else:
            st.info("💡 知识库：空（问答后自动积累）")

def show_overview(overview):
    st.subheader("项目概览")
    st.caption(f"Commit {overview.commit_sha} · {overview.file_count} 个快照文件 · {overview.python_file_count} 个 Python 文件")
    st.markdown("**目录**")
    st.dataframe([{"目录": item.name, "文件数": item.file_count} for item in overview.directories], hide_index=True)
    if overview.directories_truncated:
        st.caption("目录仅列出前 30 项")
    st.markdown("**入口线索**")
    labels = {"main_guard": "Python 启动判断", "package_main": "Python 包入口文件", "streamlit_page": "Streamlit 页面配置"}
    for clue in overview.entry_clues:
        source = clue.evidence
        label = re.sub(r"([\\\[\]])", r"\\\1", source.label).replace("\n", " ").replace("\r", " ")
        st.markdown(f"[{label}]({source.url}) · {labels[clue.kind]}")
        with st.expander(label):
            st.code(source.numbered_source, language=None)
    if not overview.entry_clues:
        st.write("未找到已支持规则的入口线索")
    if overview.entry_clues_truncated:
        st.caption("入口线索仅列出前 20 项")
    with st.expander("Python 模块"):
        st.dataframe([{"文件": item.file_path, "顶层定义": ", ".join(item.definitions) + (" …" if item.definitions_truncated else ""),
                       "导入模块": ", ".join(item.imports) + (" …" if item.imports_truncated else ""),
                       "文档摘录": item.docstring_excerpt} for item in overview.modules], hide_index=True)
        if overview.modules_truncated:
            st.caption("模块仅列出前 30 项")
    if overview.configuration_files:
        with st.expander("配置与依赖文件"):
            st.text("\n".join(overview.configuration_files))
            if overview.configuration_files_truncated:
                st.caption("文件仅列出前 30 项")
    if overview.readme:
        with st.expander("README 摘录"):
            st.code(overview.readme.numbered_source, language=None)
    if overview.scan_limited or overview.skipped_count:
        st.caption(f"扫描受限：{'是' if overview.scan_limited else '否'} · 跳过 {overview.skipped_count} 个文件")
        reasons = {"file_too_large": "文件超过读取上限", "binary_file": "非文本内容",
                   "python_parse_error": "Python 语法无法解析", "entry_excerpt_too_long": "入口行超过引用上限",
                   "readme_excerpt_too_long": "README 首行超过引用上限"}
        for item in overview.skipped_examples:
            st.text(f"{item.file_path}: {reasons[item.reason]}")


if "repository_index" in st.session_state:
    if st.button("查看项目概览", key="show_overview"):
        repository = st.session_state["repository_index"]
        try:
            result = build_repository_overview(repository.repo_root, repository.repo_url, repository.commit_sha)
            if result.repo_url != repository.repo_url or result.commit_sha != repository.commit_sha:
                raise ValueError("概览与当前仓库或代码版本不一致。")
            st.session_state["repo_overview"] = result
        except (ValueError, OSError) as exc:
            st.session_state.pop("repo_overview", None)
            st.error(f"概览生成失败：{exc}")
    if "repo_overview" in st.session_state:
        show_overview(st.session_state["repo_overview"])

# --- 对话区 ---
if "repository_index" in st.session_state:
    answer_mode = st.radio("任务", ["问答", "修改规划"], horizontal=True, key="answer_mode")

if "messages" not in st.session_state:
    st.session_state["messages"] = []

# 显示历史消息
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("sources"):
            show_sources(CheckedAnswer.model_validate(msg["sources"]))

# 输入框
if question := st.chat_input("问一个关于代码的问题..."):
    if "repo_url" not in st.session_state:
        st.warning("请先在左侧索引一个仓库")
    else:
        # 显示用户消息
        with st.chat_message("user"):
            st.markdown(question)

        try:
            repository = st.session_state["repository_index"]
            if "conversation_id" not in st.session_state:
                st.session_state["conversation_id"] = create_conversation(
                    repository.repo_url, repository.commit_sha, repository.collection_name,
                )
            with st.chat_message("assistant"):
                checked_results = []
                response = st.write_stream(ask_stream(
                    question,
                    repository.repo_url,
                    repository.repo_root,
                    collection_name=repository.collection_name,
                    commit_sha=repository.commit_sha,
                    conversation_id=st.session_state["conversation_id"],
                    mode="plan" if answer_mode == "修改规划" else "qa",
                    on_result=checked_results.append,
                ))
                checked = checked_results[0] if checked_results else None
                show_sources(checked)
            st.session_state["messages"].extend([
                {"role": "user", "content": question},
                {"role": "assistant", "content": response,
                 "sources": checked.model_dump(mode="json") if checked else None},
            ])
        except Exception as exc:
            st.error(f"回答失败：{exc}")
