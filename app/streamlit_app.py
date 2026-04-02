"""Main Streamlit entry point - tab-based layout."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from src.database import init_db

# Initialise database on startup
init_db()

st.set_page_config(
    page_title="Stripe Accounting Dashboard",
    page_icon="S",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Sidebar: project description only ---
with st.sidebar:
    st.title("Stripe Accounting")
    st.markdown(
        "Automated payment classification and quarterly reporting. "
        "Classifies Stripe payments by activity type and geographic region, "
        "generates Excel reports, and tracks invoice uploads."
    )
    st.caption("v2.0")

# --- Main content: horizontal tabs ---
tab_report, tab_browser, tab_history, tab_config, tab_invoices = st.tabs([
    "Quarter Report",
    "Transaction Browser",
    "History & Charts",
    "Configuration",
    "Invoice Upload",
])

from app.quarter_report import render as render_quarter_report
from app.transaction_browser import render as render_transaction_browser
from app.history import render as render_history
from app.configuration import render as render_configuration
from app.invoice_upload import render as render_invoice_upload

with tab_report:
    render_quarter_report()

with tab_browser:
    render_transaction_browser()

with tab_history:
    render_history()

with tab_config:
    render_configuration()

with tab_invoices:
    render_invoice_upload()
