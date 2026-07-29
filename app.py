import asyncio
import streamlit as st
import nest_asyncio

# uvloop (installed as a dep by chromadb/pydantic-ai) is incompatible with
# nest_asyncio — reset to the standard asyncio policy before patching.
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
nest_asyncio.apply()

from indexer import clone_repo, chunk_repo, index_chunks, is_indexed
from agent.qa_agent import ask_stream
from memory.knowledge import get_memory_stats

st.set_page_config(page_title="Codebase QA", page_icon="🔍", layout="wide")
st.title("🔍 Codebase Q&A")
st.caption("输入 GitHub 仓库地址，用自然语言提问代码相关问题")

# --- 仓库索引区 ---
with st.sidebar:
    st.subheader("📦 仓库索引")
    repo_url = st.text_input(
        "GitHub 仓库地址",
        placeholder="https://github.com/owner/repo",
    )

    if repo_url:
        already_indexed = is_indexed(repo_url)
        if already_indexed:
            st.success("已索引 ✓")
            if st.button("重新索引"):
                st.session_state.pop("repo_url", None)
                st.session_state.pop("repo_root", None)
                already_indexed = False

        if not already_indexed:
            if st.button("克隆并索引", type="primary"):
                with st.spinner("克隆仓库中（浅克隆 depth=1）..."):
                    try:
                        repo_root = clone_repo(repo_url)
                        st.session_state["repo_root"] = repo_root
                    except Exception as e:
                        st.error(f"克隆失败：{e}")
                        st.stop()

                progress = st.progress(0, text="解析代码文件中（AST + 滑动窗口）...")
                chunks = []
                for chunk in chunk_repo(repo_root):
                    chunks.append(chunk)
                    if len(chunks) % 200 == 0:
                        progress.progress(
                            min(30, len(chunks) // 10),
                            text=f"已解析 {len(chunks)} 个代码块...",
                        )
                progress.progress(30, text=f"已解析 {len(chunks)} 个代码块")

                progress.progress(40, text=f"正在计算 embedding 向量（{len(chunks)} 个代码块，CPU 推理）...")

                def update_progress(done, total):
                    pct = 40 + int(55 * done / total) if total else 95
                    progress.progress(pct, text=f"已索引 {done}/{total} 个代码块...")

                index_chunks(chunks, repo_url, progress_callback=update_progress)
                progress.progress(100, text="索引完成！")

                st.session_state["repo_url"] = repo_url
                st.success(f"索引完成，共 {len(chunks)} 个代码块")
                st.rerun()
        else:
            if "repo_url" not in st.session_state:
                from indexer.cloner import clone_repo as _clone
                repo_root = _clone(repo_url)
                st.session_state["repo_url"] = repo_url
                st.session_state["repo_root"] = repo_root

    st.divider()
    st.caption("支持的文件类型：.py .js .ts .go .java .rs .cpp .rb .swift 等")

    # 知识库统计
    if "repo_url" in st.session_state:
        stats = get_memory_stats(st.session_state["repo_url"])
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
            response = st.write_stream(ask_stream(
                question,
                st.session_state["repo_url"],
                st.session_state["repo_root"],
            ))

        st.session_state["messages"].append({"role": "assistant", "content": response})
