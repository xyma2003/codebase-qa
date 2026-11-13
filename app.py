import re
import streamlit as st
from security.output import safe_answer_html, redact_text, redact_data, safe_error, valid_source_url, install_log_redaction

install_log_redaction()

from indexer.repository_service import (
    get_current_repository, get_repository_version, import_repository,
    list_repository_versions,
)
from indexer.snapshots import normalize_repo_url
from agent.qa_agent import ask_stream
from agent.ui_runtime import run_ui_stream
from memory.knowledge import get_memory_stats
from agent.evidence import CheckedAnswer
from agent.conversations import create_conversation, list_conversations, load_conversation, display_messages
from indexer.overview import build_repository_overview
from agent.runtime import RunPolicy

st.set_page_config(page_title="Codebase QA", page_icon="🔍", layout="wide")
from security.web_auth import require_web_user

require_web_user()
st.title("🔍 Codebase Q&A")
st.caption("输入 GitHub 仓库地址，用自然语言提问代码相关问题")


def show_sources(result):
    if result is None:
        return
    with st.expander("源码引用"):
        for citation in result.citations:
            label = re.sub(r"([\\\[\]])", r"\\\1", citation.label).replace("\n", " ").replace("\r", " ")
            if valid_source_url(citation.url):
                st.markdown(f"[{citation.reference_id} · {label}]({citation.url})")
            else:
                st.text(redact_text(citation.label))
            st.caption(f"Commit {citation.commit_sha}")
            st.code(redact_text(citation.numbered_source), language=None)
        for issue in result.issues:
            st.text(redact_text(f"{issue.reference_id}：{issue.message}" if issue.reference_id else issue.message))


def show_answer(text, checked=None):
    urls = [citation.url for citation in checked.citations] if checked else []
    st.html(safe_answer_html(text, source_urls=urls), unsafe_allow_javascript=False)


def show_tool_events(events, *, expanded=False):
    if not events:
        return
    states = {"running": "正在执行", "returned": "已返回", "failed": "执行失败"}
    with st.expander("工具调用", expanded=expanded):
        for event in events:
            duration = f" · {event.elapsed_ms} ms" if event.elapsed_ms is not None else ""
            size = f" · 返回 {event.output_chars} 字符" if event.output_chars is not None else ""
            st.text(f"{event.tool_name} · {states[event.status]}{duration}{size}")
            st.json(redact_data(event.arguments), expanded=False)


def show_runtime(record):
    if record is None:
        return
    with st.expander("运行记录"):
        st.text(f"{record.requests} 次模型请求 · {record.tool_calls} 次工具调用 · {record.elapsed_ms} ms")
        st.text(f"输入 {record.input_tokens} Token · 输出 {record.output_tokens} Token")
        st.json({"retrieval_strategy": record.retrieval_strategy, "use_answer_cache": record.use_answer_cache,
                 "limits": record.policy.model_dump() if record.policy else None,
                 "enforced_limits": record.enforced_limits}, expanded=False)


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
        st.error(f"导入失败：{safe_error(exc)}")
    finally:
        progress.empty()

# --- 仓库索引区 ---
with st.sidebar:
    st.subheader("📦 仓库索引")
    repo_url = st.text_input(
        "GitHub 仓库地址",
        value=st.session_state.get("repo_url", ""),
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
                    st.session_state["history_selector_epoch"] = st.session_state.get("history_selector_epoch", 0) + 1
                    st.rerun()
                repository = st.session_state["repository_index"]
                try:
                    histories = list_conversations(repository.repo_url, repository.commit_sha, repository.collection_name)
                except (ValueError, OSError) as exc:
                    histories = []
                    st.error(f"读取历史失败：{safe_error(exc)}")
                if histories:
                    labels = {item["conversation_id"]: f"{item['turn_count']} 轮 · {redact_text(item['title'])[:60]}" for item in histories}
                    choices = [None, *labels]
                    active = st.session_state.get("conversation_id")
                    selected_history = st.selectbox("历史对话", choices, index=choices.index(active) if active in choices else 0,
                        format_func=lambda value: labels[value] if value else "选择对话",
                        key=f"history_{repository.collection_name}_{st.session_state.get('history_selector_epoch', 0)}")
                    if selected_history and selected_history != active:
                        try:
                            conversation = load_conversation(selected_history, repository.repo_url, repository.commit_sha, repository.collection_name)
                            restored = display_messages(conversation)
                        except (ValueError, OSError) as exc:
                            st.error(f"读取历史失败：{safe_error(exc)}")
                        else:
                            st.session_state["messages"] = restored
                            st.session_state["conversation_id"] = selected_history
                            st.session_state["answer_mode"] = "修改规划" if conversation.turns[-1].answer.plan else "问答"
        except (ValueError, OSError) as exc:
            clear_repository()
            st.error(safe_error(exc))
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
        st.caption(f"历史答案：已批准 {stats['count']} · 待审核 {stats.get('pending_count', 0)}")

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
            st.code(redact_text(source.numbered_source), language=None)
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
            st.code(redact_text(overview.readme.numbered_source), language=None)
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
            st.error(f"概览生成失败：{safe_error(exc)}")
    if "repo_overview" in st.session_state:
        show_overview(st.session_state["repo_overview"])

# --- 对话区 ---
if "repository_index" in st.session_state:
    answer_mode = st.radio("任务", ["问答", "修改规划"], horizontal=True, key="answer_mode")
    with st.expander("运行设置"):
        use_answer_cache = st.checkbox("使用历史答案缓存", value=True, key="use_answer_cache")
        retrieval_label = st.selectbox("检索方式", ["向量检索", "融合检索（实验）"], key="retrieval_strategy")
        retrieval_strategy = "hybrid" if retrieval_label == "融合检索（实验）" else "semantic"
        policy = None
        if st.checkbox("设置运行限制", key="use_run_limits"):
            request_limit = st.number_input("每轮模型请求上限", min_value=1, max_value=50, value=50, step=1, key="request_limit")
            tool_limit = st.number_input("每轮工具调用上限", min_value=0, max_value=50, value=0, step=1, help="0 表示采用安全上限", key="tool_limit")
            token_limit = st.number_input("每轮总 Token 上限", min_value=0, value=0, step=100, help="按模型返回用量检查；0 表示不设限", key="token_limit")
            timeout = st.number_input("每轮超时秒数", min_value=0, max_value=300, value=0, step=1, help="0 表示采用安全上限", key="timeout_seconds")
            input_limit = st.number_input("问题与历史字符上限", min_value=0, value=0, step=1000, help="不含系统指令、工具定义和本轮新工具结果；0 表示只采用安全字节上限", key="input_limit")
            policy = RunPolicy(request_limit=request_limit, tool_calls_limit=tool_limit or None,
                               total_tokens_limit=token_limit or None, timeout_seconds=float(timeout) if timeout else None,
                               input_characters_limit=input_limit or None)

if "messages" not in st.session_state:
    st.session_state["messages"] = []

# 显示历史消息
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        checked = CheckedAnswer.model_validate(msg["sources"]) if msg.get("sources") else None
        if msg["role"] == "assistant":
            show_answer(msg["content"], checked)
        else:
            st.text(redact_text(msg["content"]))
        if msg.get("sources"):
            saved = CheckedAnswer.model_validate(msg["sources"])
            show_tool_events(saved.tool_events)
            show_runtime(saved.runtime)
            show_sources(saved)

# 输入框
if question := st.chat_input("问一个关于代码的问题..."):
    if "repo_url" not in st.session_state:
        st.warning("请先在左侧索引一个仓库")
    else:
        # 显示用户消息
        with st.chat_message("user"):
            st.text(redact_text(question))

        try:
            repository = st.session_state["repository_index"]
            if "conversation_id" not in st.session_state:
                st.session_state["conversation_id"] = create_conversation(
                    repository.repo_url, repository.commit_sha, repository.collection_name,
                )
            with st.chat_message("assistant"):
                checked_results = []
                running_events = {}
                event_view = st.empty()
                def on_event(event):
                    running_events[event.call_id] = event
                    with event_view.container():
                        show_tool_events(list(running_events.values()), expanded=True)
                conversation_id = st.session_state["conversation_id"]
                response = run_ui_stream(lambda observe: ask_stream(
                    question,
                    repository.repo_url,
                    repository.repo_root,
                    collection_name=repository.collection_name,
                    commit_sha=repository.commit_sha,
                    conversation_id=conversation_id,
                    mode="plan" if answer_mode == "修改规划" else "qa",
                    on_result=checked_results.append,
                    on_event=observe,
                    retrieval_strategy=retrieval_strategy,
                    use_answer_cache=use_answer_cache,
                    policy=policy,
                ), on_event=on_event)
                checked = checked_results[0] if checked_results else None
                event_view.empty()
                show_answer(response, checked)
                if checked:
                    show_tool_events(checked.tool_events)
                    show_runtime(checked.runtime)
                show_sources(checked)
            st.session_state["messages"].extend([
                {"role": "user", "content": question},
                {"role": "assistant", "content": response,
                 "sources": checked.model_dump(mode="json") if checked else None},
            ])
        except Exception as exc:
            st.error(f"回答失败：{safe_error(exc)}")
