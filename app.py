import asyncio
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

st.set_page_config(page_title="Codebase QA", page_icon="🔍", layout="wide")
st.title("🔍 Codebase Q&A")
st.caption("输入 GitHub 仓库地址，用自然语言提问代码相关问题")


def clear_repository():
    for key in ("repository_index", "repo_url", "repo_root"):
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

# --- 对话区 ---
if "messages" not in st.session_state:
    st.session_state["messages"] = []

# 显示历史消息
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 输入框
if question := st.chat_input("问一个关于代码的问题..."):
    if "repo_url" not in st.session_state:
        st.warning("请先在左侧索引一个仓库")
    else:
        # 显示用户消息
        st.session_state["messages"].append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        # 流式输出 Agent 回复 — st.write_stream handles markdown rendering (code blocks, etc.)
        with st.chat_message("assistant"):
            repository = st.session_state["repository_index"]
            response = st.write_stream(ask_stream(
                question,
                st.session_state["repo_url"],
                st.session_state["repo_root"],
                collection_name=repository.collection_name,
                commit_sha=repository.commit_sha,
            ))

        st.session_state["messages"].append({"role": "assistant", "content": response})
