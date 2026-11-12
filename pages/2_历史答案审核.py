import streamlit as st
from memory.review_ui import render_cache_review

st.set_page_config(page_title="历史答案审核", layout="wide")
render_cache_review()
