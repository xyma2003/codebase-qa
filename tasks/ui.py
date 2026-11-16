"""Small status panel; no Streamlit context is installed on a worker thread."""
import streamlit as st

from security.output import safe_error
from tasks.store import LIVE

STATES = {"queued": "排队中", "running": "执行中", "cancelling": "正在取消", "succeeded": "已完成",
          "failed": "失败", "cancelled": "已取消", "interrupted": "已中断"}


@st.fragment(run_every=1)
def task_panel():
    from tasks import service as services
    # Don't create a worker simply to view an empty workspace.
    if services._singleton is None and not (services.ROOT / ".baseline_runtime/tasks/tasks.sqlite3").exists():
        return
    # A fragment reruns independently of the page authentication gate.
    from security.web_auth import callback_principal
    try:
        principal = callback_principal()
        if st.session_state.get("_verified_account") != principal.owner_id + ":" + principal.role:
            st.rerun()
        service = services.task_service()
        tasks = service.list()
        active = st.session_state.get("active_task")
        if active:
            state = service.get(active)["state"]
            if state not in LIVE:
                st.rerun()
        if not tasks:
            return
        with st.sidebar.expander("任务", expanded=bool(active)):
            for task in tasks:
                kind = "导入" if task["kind"] == "import" else "回答"
                st.text(f"{kind} · {task['id'][:8]} · {STATES[task['state']]}")
                st.text(task["title"])
                progress = task["progress"]
                if progress.get("total"):
                    st.progress(min(1., progress["done"] / progress["total"]))
                elif progress.get("tool_name"):
                    st.text(progress["tool_name"])
                if task["error"]:
                    st.text(task["error"])
                if task["state"] in LIVE and st.button("取消", key="task_cancel_" + task["id"]):
                    service.cancel(task["id"])
                    st.rerun()
                if task["state"] == "succeeded" and st.button("查看结果", key="task_open_" + task["id"]):
                    st.session_state["open_task"] = task["id"]
                    st.rerun()
    except Exception as exc:
        st.error(safe_error(exc))
