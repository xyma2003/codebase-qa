import asyncio
import streamlit as st
import nest_asyncio

nest_asyncio.apply()

from indexer import clone_repo, chunk_repo, index_chunks, is_indexed
from agent.qa_agent import ask_stream

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
                with st.spinner("克隆仓库中..."):
                    try:
                        repo_root = clone_repo(repo_url)
                        st.session_state["repo_root"] = repo_root
                    except Exception as e:
                        st.error(f"克隆失败：{e}")
                        st.stop()

                progress = st.progress(0, text="解析代码文件...")
                chunks = list(chunk_repo(repo_root))
                progress.progress(50, text=f"已解析 {len(chunks)} 个代码块，正在建立向量索引...")

                index_chunks(chunks, repo_url)
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

        # 流式输出 Agent 回复
        with st.chat_message("assistant"):
            response_placeholder = st.empty()
            full_response = ""

            async def stream_response():
                nonlocal full_response
                async for chunk in ask_stream(
                    question,
                    st.session_state["repo_url"],
                    st.session_state["repo_root"],
                ):
                    full_response += chunk
                    response_placeholder.markdown(full_response + "▌")
                response_placeholder.markdown(full_response)

            asyncio.run(stream_response())

        st.session_state["messages"].append({"role": "assistant", "content": full_response})
