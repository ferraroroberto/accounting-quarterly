"""Main Streamlit entry point for Stripe Automation Dashboard."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

st.set_page_config(
    page_title="Stripe Automation Dashboard",
    page_icon="💳",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.sidebar.title("💳 Stripe Dashboard")
st.sidebar.markdown("---")

pages = {
    "📊 Quarter Report": "pages/01_Quarter_Report.py",
    "🔍 Transaction Browser": "pages/02_Transaction_Browser.py",
    "✅ Validation": "pages/03_Validation.py",
    "⚙️ Configuration": "pages/04_Configuration.py",
    "📈 History": "pages/05_History.py",
}

st.title("💳 Stripe Automation Dashboard")
st.markdown(
    """
    Welcome! Use the sidebar to navigate between sections:

    | Page | Purpose |
    |------|---------|
    | **📊 Quarter Report** | View and export quarterly income summaries |
    | **🔍 Transaction Browser** | Search, filter and manually override classifications |
    | **✅ Validation** | Compare against historical known totals |
    | **⚙️ Configuration** | Manage API keys and client mappings |
    | **📈 History** | Timeline view of all quarters |
    """
)

st.info("👈 Select a page from the sidebar to get started.")
